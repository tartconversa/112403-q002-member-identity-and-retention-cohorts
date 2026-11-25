"""Cohort/region/category repurchase analysis and impact register."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .consent import ConsentLedger
from .dates import cohort_period, elapsed_days, parse_as_of
from .identity import IdentityGraph, IdentityResolution
from .io import InputData
from .models import CustomerMetric, Order, ValidOrder
from .orders import build_valid_orders, is_effectively_valid

UNKNOWN_REGION = "未提供地区"
MULTIPLE_REGION = "多个地区"
UNKNOWN_CATEGORY = "未分类"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass
class AnalysisResult:
    as_of: str
    cohort_grain: str
    window_days: int
    horizons: list[int]
    data_hash: str
    customers: list[CustomerMetric]
    segments: list[dict[str, Any]]
    common_period: list[dict[str, Any]]
    impacts: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "as_of": self.as_of,
                "cohort_grain": self.cohort_grain,
                "window_days": self.window_days,
                "horizons": self.horizons,
                "data_hash": self.data_hash,
                "summary": self.summary,
                "customers": [asdict(customer) for customer in self.customers],
                "segments": self.segments,
                "common_period": self.common_period,
                "impacts": self.impacts,
            }
        )


def _dominant_category(order: Order, items_by_order: dict[str, list]) -> str:
    if order.category:
        return order.category
    items = items_by_order.get(order.order_id, [])
    if not items:
        return UNKNOWN_CATEGORY
    amounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: Counter[str] = Counter()
    for item in items:
        counts[item.category] += 1
        if item.amount is not None:
            amounts[item.category] += item.amount
    if any(amounts.values()):
        best_amount = max(amounts.values())
        categories = sorted(cat for cat, amount in amounts.items() if amount == best_amount)
    else:
        best_count = max(counts.values())
        categories = sorted(cat for cat, count in counts.items() if count == best_count)
    return categories[0] if categories else UNKNOWN_CATEGORY


def _region_for_group(
    person_id: str | None,
    members: set[str],
    data: InputData,
) -> tuple[str, list[str]]:
    flags: list[str] = []
    person_profiles = {p.person: p for p in data.persons}
    member_profiles = {m.member: m for m in data.members}

    if person_id and person_id in person_profiles and person_profiles[person_id].region:
        return person_profiles[person_id].region or UNKNOWN_REGION, flags

    regions = [member_profiles[m].region for m in members if m in member_profiles and member_profiles[m].region]
    if not regions:
        # Fall back to order-declared region when no customer profile exists.
        regions = [o.region for o in data.orders if o.member in members and o.region]
    unique = sorted(set(regions))
    if not unique:
        return UNKNOWN_REGION, flags
    if len(unique) > 1:
        flags.append("region_conflict_using_multiple_bucket")
        return MULTIPLE_REGION, flags
    return unique[0], flags


def _joined_for_group(
    person_id: str | None,
    members: set[str],
    data: InputData,
    grain: str,
) -> tuple[Any, str, list[str]]:
    flags: list[str] = []
    person_profiles = {p.person: p for p in data.persons}
    member_profiles = {m.member: m for m in data.members}

    if person_id and person_id in person_profiles and person_profiles[person_id].joined_at:
        joined = person_profiles[person_id].joined_at
        return joined, cohort_period(joined, grain), flags

    member_dates = [member_profiles[m].joined_at for m in members if m in member_profiles and member_profiles[m].joined_at]
    if member_dates:
        joined = min(member_dates)
        if len({d for d in member_dates}) > 1:
            flags.append("multiple_member_join_dates_using_earliest")
        return joined, cohort_period(joined, grain), flags

    identity_dates = [link.valid_from for link in data.identity if link.member in members]
    if identity_dates:
        joined = min(identity_dates)
        flags.append("cohort_derived_from_identity_valid_from")
        return joined, cohort_period(joined, grain), flags

    flags.append("missing_join_date")
    return None, cohort_period(None, grain), flags


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def run_analysis(
    data: InputData,
    as_of: str | datetime | None = None,
    horizons: Iterable[int] = (30, 60, 90),
    cohort_grain: str = "month",
    window_days: int = 90,
) -> AnalysisResult:
    cutoff = parse_as_of(as_of)
    horizon_list = sorted(set(int(h) for h in horizons))
    if window_days not in horizon_list:
        horizon_list = sorted(horizon_list + [window_days])

    graph = IdentityGraph(data.identity)
    consent = ConsentLedger(data.consent)
    items_by_order: dict[str, list] = defaultdict(list)
    for item in data.order_items:
        items_by_order[item.order_id].append(item)

    visible_orders = [order for order in data.orders if order.at <= cutoff]
    visible_refunds = [refund for refund in data.refunds if refund.at <= cutoff]
    valid_order_map = build_valid_orders(visible_orders, visible_refunds)

    # Assign every order using identity truth valid on that order's date.
    order_group: dict[str, str] = {}
    order_resolution: dict[str, IdentityResolution] = {}
    groups: dict[str, dict[str, Any]] = {}

    def ensure_group(key: str) -> dict[str, Any]:
        if key not in groups:
            person_id = key[2:] if key.startswith("P:") else None
            groups[key] = {
                "members": set(),
                "orders": [],
                "person_id": person_id,
                "event_resolutions": [],
                "current_resolutions": [],
            }
        return groups[key]

    universe_members = {m.member for m in data.members}
    universe_members.update(o.member for o in data.orders)
    universe_members.update(link.member for link in data.identity)

    member_to_groups: dict[str, set[str]] = defaultdict(set)
    for order in visible_orders:
        resolution = graph.resolve(order.member, order.at)
        order_resolution[order.order_id] = resolution
        key = f"P:{resolution.person}" if resolution.person else f"M:{order.member}"
        order_group[order.order_id] = key
        group = ensure_group(key)
        group["members"].add(order.member)
        group["orders"].append(order.order_id)
        group["event_resolutions"].append(resolution)
        member_to_groups[order.member].add(key)

    # Roster members ensure join-only members are visible.  Do not create a
    # duplicate expired/current M-group for a member already represented by an
    # event-time P-group: attach current governance state to that group.
    for member in sorted(universe_members):
        resolution = graph.resolve(member, cutoff)
        existing_keys = member_to_groups.get(member, set())
        if existing_keys:
            for key in existing_keys:
                group = groups[key]
                group["members"].add(member)
                group["current_resolutions"].append(resolution)
                if resolution.person and not group["person_id"]:
                    group["person_id"] = resolution.person
        else:
            key = f"P:{resolution.person}" if resolution.person else f"M:{member}"
            group = ensure_group(key)
            group["members"].add(member)
            group["event_resolutions"].append(resolution)
            group["current_resolutions"].append(resolution)
            member_to_groups[member].add(key)
            if resolution.person and not group["person_id"]:
                group["person_id"] = resolution.person

    customers: list[CustomerMetric] = []
    impacts: list[dict[str, Any]] = []

    for customer_id in sorted(groups):
        group = groups[customer_id]
        members: set[str] = group["members"]
        assigned_order_ids: list[str] = sorted(group["orders"])
        person_id: str | None = group["person_id"]

        event_resolutions = group["event_resolutions"]
        current_resolutions = group["current_resolutions"] or event_resolutions
        statuses = [r.status for r in event_resolutions]
        current_statuses = [r.status for r in current_resolutions]
        flags = sorted(
            {
                flag
                for r in [*event_resolutions, *current_resolutions]
                for flag in r.flags
            }
        )
        possible_persons = sorted(
            {
                p
                for r in [*event_resolutions, *current_resolutions]
                for p in r.possible_persons
            }
        )
        if person_id:
            possible_persons = sorted(set(possible_persons) | {person_id})

        precedence = {"conflict": 4, "disputed": 3, "incomplete": 2, "revoked": 1, "confirmed": 0}
        identity_status = max(statuses or ["incomplete"], key=lambda s: precedence.get(s, 0))
        current_identity_status = max(current_statuses or ["incomplete"], key=lambda s: precedence.get(s, 0))
        identity_unresolved = identity_status != "confirmed"

        joined_at, cohort_period_value, cohort_flags = _joined_for_group(person_id, members, data, cohort_grain)
        region, region_flags = _region_for_group(person_id, members, data)
        flags.extend(cohort_flags)
        flags.extend(region_flags)

        records = [valid_order_map[oid] for oid in assigned_order_ids if oid in valid_order_map]
        valid_records = [record for record in records if is_effectively_valid(record)]
        valid_records.sort(key=lambda r: (r.order.at, r.order.order_id))
        excluded_ids = [r.order.order_id for r in records if not is_effectively_valid(r)]

        first = valid_records[0] if valid_records else None
        second = valid_records[1] if len(valid_records) > 1 else None
        first_order = first.order if first else None

        category = _dominant_category(first_order, items_by_order) if first_order else UNKNOWN_CATEGORY
        first_date = first_order.at.date() if first_order else None
        observed_days = elapsed_days(first_order.at, cutoff) if first_order else 0
        window_end = first_date.fromordinal(first_date.toordinal() + window_days) if first_date else None
        mature = observed_days >= window_days if first_order else False
        days_remaining = max(0, window_days - observed_days) if first_order and not mature else 0

        days_to_repeat = None
        repeat_within_window = False
        second_order_id = None
        second_date = None
        if second and first_order:
            gap = elapsed_days(first_order.at, second.order.at)
            if gap <= window_days:
                repeat_within_window = True
                days_to_repeat = gap
                second_order_id = second.order.order_id
                second_date = second.order.at.date()

        membership_age = elapsed_days(joined_at, cutoff) if joined_at else None

        if not first:
            repeat_status = "no_valid_order"
        elif repeat_within_window:
            repeat_status = "repeat"
        elif identity_unresolved and mature:
            repeat_status = "indeterminate"
        elif mature:
            repeat_status = "no_repeat"
        else:
            repeat_status = "observing"

        # Bounded "could a disputed graph add an order?" simulation.
        closure_members, closure_persons = graph.possible_closure(set(members), set(possible_persons))
        candidate_order_ids = [
            order.order_id
            for order in visible_orders
            if order.member in closure_members
            or any(
                (graph.resolve(order.member, order.at).person in closure_persons)
                or bool(set(graph.resolve(order.member, order.at).possible_persons) & closure_persons)
                for _ in [0]
            )
        ]
        extra_order_ids = sorted(set(candidate_order_ids) - set(assigned_order_ids))
        candidate_records = [
            valid_order_map[oid]
            for oid in candidate_order_ids
            if oid in valid_order_map and is_effectively_valid(valid_order_map[oid])
        ]
        candidate_records.sort(key=lambda r: (r.order.at, r.order.order_id))
        possible_repeat = False
        if len(candidate_records) >= 2:
            possible_repeat = elapsed_days(candidate_records[0].order.at, candidate_records[1].order.at) <= window_days
        unbounded_identity = any(
            "member_missing_from_identity_registry" in r.flags or "identity_link_not_valid_at_time" in r.flags
            for r in event_resolutions
        )
        identity_material = bool(
            identity_unresolved
            and not repeat_within_window
            and (unbounded_identity or bool(extra_order_ids) or possible_repeat)
        )

        consent_subjects = [person_id] if person_id else possible_persons
        active_channels: set[str] = set()
        withdrawn_channels: set[str] = set()
        for subject in filter(None, consent_subjects):
            active, withdrawn = consent.current_channels(subject, cutoff)
            active_channels.update(active)
            withdrawn_channels.update(withdrawn)

        metric = CustomerMetric(
            customer_id=customer_id,
            person_id=person_id,
            member_ids=sorted(members),
            cohort_period=cohort_period_value,
            joined_at=joined_at,
            region=region,
            category=category,
            identity_status=identity_status,
            current_identity_status=current_identity_status,
            identity_flags=sorted(set(flags)),
            possible_person_ids=possible_persons,
            first_order_id=first_order.order_id if first_order else None,
            first_order_at=first_date,
            first_order_net_amount=str(first.net_amount) if first else None,
            second_order_id=second_order_id,
            second_order_at=second_date,
            days_to_repeat=days_to_repeat,
            observed_days=observed_days,
            membership_age_days=membership_age,
            window_days=window_days,
            window_end=window_end,
            mature=mature,
            days_remaining=days_remaining if first_order and not mature else None,
            repeat_status=repeat_status,
            repeat_within_window=repeat_within_window if first_order else None,
            identity_material=identity_material,
            possible_extra_order_ids=extra_order_ids,
            consent_active_channels=sorted(active_channels),
            consent_withdrawn_channels=sorted(withdrawn_channels),
            order_count_valid=len(valid_records),
            excluded_order_ids=excluded_ids,
            data_flags=sorted(set(flags)),
        )
        customers.append(metric)

        segment = metric.segment_key()
        if identity_unresolved:
            impacts.append(
                {
                    "impact_id": f"identity:{customer_id}",
                    "trigger_type": f"identity_{identity_status}",
                    "subject": person_id or customer_id,
                    "customer_id": customer_id,
                    "segment_cohort": segment[0],
                    "segment_region": segment[1],
                    "segment_category": segment[2],
                    "affected_customer_metric": True,
                    "affected_segment_metrics": [
                        "indeterminate_customers",
                        "repurchase_rate_decided",
                        "repurchase_rate_lower_bound",
                        "repurchase_rate_upper_bound",
                    ],
                    "affected_list_ids": [],
                    "owner": None,
                    "member_ids": sorted(members),
                    "possible_person_ids": possible_persons,
                    "severity": "high" if identity_status in {"conflict", "incomplete"} else "medium",
                    "detail": "；".join(flags) or "identity relationship is not fully confirmed",
                    "possible_extra_orders": extra_order_ids,
                    "unbounded_identity": unbounded_identity,
                }
            )
        elif current_identity_status != "confirmed":
            impacts.append(
                {
                    "impact_id": f"identity-current:{customer_id}",
                    "trigger_type": f"current_identity_{current_identity_status}",
                    "subject": person_id or customer_id,
                    "customer_id": customer_id,
                    "member_ids": sorted(members),
                    "segment_cohort": segment[0],
                    "segment_region": segment[1],
                    "segment_category": segment[2],
                    "affected_customer_metric": False,
                    "affected_segment_metrics": ["campaign_eligibility", "current_reachable_identity_count"],
                    "affected_list_ids": [],
                    "owner": None,
                    "severity": "medium",
                    "detail": "Historical order-time links are usable, but the relationship is not confirmed at the review cutoff.",
                    "possible_extra_orders": extra_order_ids,
                    "unbounded_identity": unbounded_identity,
                }
            )

    # Consent withdrawal impacts are attached after customer metrics exist.
    customer_by_person: dict[str, CustomerMetric] = {}
    for metric in customers:
        for subject in [metric.person_id, *metric.possible_person_ids]:
            if subject:
                customer_by_person.setdefault(subject, metric)

    for event in consent.withdrawals(cutoff):
        metric = customer_by_person.get(event.person)
        segment = metric.segment_key() if metric else (None, None, None)
        impacts.append(
            {
                "impact_id": f"consent:{event.person}:{event.channel}:{event.line}",
                "trigger_type": "consent_withdrawn",
                "subject": event.person,
                "channel": event.channel,
                "effective_at": event.at.date().isoformat(),
                "customer_id": metric.customer_id if metric else None,
                "member_ids": metric.member_ids if metric else [],
                "segment_cohort": segment[0],
                "segment_region": segment[1],
                "segment_category": segment[2],
                "affected_customer_metric": metric is not None,
                "affected_segment_metrics": ["consent_reachable_customers", "campaign_eligibility"],
                "affected_list_ids": [],
                "owner": None,
                "severity": "high",
                "detail": f"{event.channel} consent withdrawn; frozen sent records must not be rewritten",
            }
        )

    # Refund/cancellation correction impacts.  A partial refund can leave an
    # order valid while still being a correction list owners should know about.
    for order in visible_orders:
        record = valid_order_map.get(order.order_id)
        if not record:
            continue
        excluded_state = record.effective_state in {"cancelled", "fully_refunded", "zero_or_negative"}
        if not excluded_state and record.refund_total <= 0:
            continue
        group_key = order_group.get(order.order_id)
        metric = next((c for c in customers if c.customer_id == group_key), None)
        impacted_metrics = ["first_order_net_amount"]
        if excluded_state:
            impacted_metrics.extend(["first_order_date", "window_end", "repeat_customers", "mature_customers"])
        impacts.append(
            {
                "impact_id": f"order_correction:{order.order_id}",
                "trigger_type": "order_excluded" if excluded_state else "order_refund_correction",
                "subject": order.member,
                "order_id": order.order_id,
                "customer_id": metric.customer_id if metric else group_key,
                "member_ids": metric.member_ids if metric else [order.member],
                "segment_cohort": metric.cohort_period if metric else None,
                "segment_region": metric.region if metric else None,
                "segment_category": metric.category if metric else None,
                "affected_customer_metric": metric is not None,
                "affected_segment_metrics": impacted_metrics,
                "affected_list_ids": [],
                "owner": None,
                "severity": "high" if record.effective_state == "fully_refunded" else ("medium" if excluded_state else "low"),
                "detail": (
                    f"order is {record.effective_state}; "
                    f"gross={order.paid}; refund_total={record.refund_total}; net={record.net_amount}"
                ),
            }
        )

    segments, common_period = _aggregate(customers, horizon_list, window_days)
    summary = _summary(customers, segments, common_period, impacts, cutoff, horizon_list)

    return AnalysisResult(
        as_of=cutoff.date().isoformat(),
        cohort_grain=cohort_grain,
        window_days=window_days,
        horizons=horizon_list,
        data_hash=data.hash_payload(),
        customers=customers,
        segments=segments,
        common_period=common_period,
        impacts=impacts,
        summary=summary,
    )


def _aggregate(customers: list[CustomerMetric], horizons: list[int], window_days: int = 90) -> tuple[list[dict], list[dict]]:
    grouped: dict[tuple[str, str, str], list[CustomerMetric]] = defaultdict(list)
    for customer in customers:
        grouped[customer.segment_key()].append(customer)

    segment_rows: list[dict[str, Any]] = []
    common_rows: list[dict[str, Any]] = []

    for key in sorted(grouped):
        items = grouped[key]
        cohort, region, category = key
        first_order_items = [c for c in items if c.first_order_id]
        mature = [c for c in first_order_items if c.mature]
        observing = [c for c in items if c.repeat_status == "observing"]
        repeats = [c for c in items if c.repeat_status == "repeat"]
        no_repeat = [c for c in items if c.repeat_status == "no_repeat"]
        indeterminate = [c for c in items if c.repeat_status == "indeterminate"]
        no_valid = [c for c in items if c.repeat_status == "no_valid_order"]
        unresolved = [c for c in items if c.identity_status != "confirmed"]
        reachable = [c for c in items if c.consent_active_channels]
        observed = [c.observed_days for c in first_order_items]

        mature_repeats = [c for c in mature if c.repeat_status == "repeat"]
        decided_90d_denominator = len(no_repeat) + len(mature_repeats)
        known_decided_denominator = len(repeats) + len(no_repeat)
        row = {
            "cohort_period": cohort,
            "region": region,
            "category": category,
            "customers_total": len(items),
            "member_accounts": sum(len(c.member_ids) for c in items),
            "no_valid_order_customers": len(no_valid),
            "first_order_customers": len(first_order_items),
            "repeat_customers": len(repeats),
            "no_repeat_customers": len(no_repeat),
            "observing_customers": len(observing),
            "indeterminate_customers": len(indeterminate),
            "identity_unresolved_customers": len(unresolved),
            "mature_customers": len(mature),
            "window_open_customers": len(first_order_items) - len(mature),
            "pending_customers": len([c for c in observing if c.repeat_within_window is False]),
            "early_repeat_window_open": len([c for c in repeats if not c.mature]),
            "immature_share": _rate(len(first_order_items) - len(mature), len(first_order_items)),
            "decided_customers": known_decided_denominator,
            "repurchase_rate_decided": _rate(len(repeats), known_decided_denominator),
            "repurchase_rate_decided_90d": _rate(len(mature_repeats), decided_90d_denominator),
            "repurchase_rate_lower_bound_90d": _rate(len([c for c in mature if c.repeat_status == "repeat"]), len(mature)),
            "repurchase_rate_upper_bound_90d": _rate(len([c for c in mature if c.repeat_status in {"repeat", "indeterminate"}]), len(mature)),
            "consent_reachable_customers": len(reachable),
            "min_observed_days": min(observed) if observed else None,
            "max_observed_days": max(observed) if observed else None,
            "complete_observation_days": min([window_days, min(observed)] if observed else [0]),
            "common_horizon_days": min([window_days, min(observed)] if observed else [0]),
        }
        segment_rows.append(row)

        for horizon in horizons:
            eligible = [c for c in first_order_items if c.observed_days >= horizon]
            h_repeat = [c for c in eligible if c.days_to_repeat is not None and c.days_to_repeat <= horizon]
            h_indeterminate = [
                c
                for c in eligible
                if not (c.days_to_repeat is not None and c.days_to_repeat <= horizon)
                and c.identity_status != "confirmed"
            ]
            h_no_repeat = [
                c
                for c in eligible
                if c not in h_repeat and c not in h_indeterminate
            ]
            decided_denominator = len(h_repeat) + len(h_no_repeat)
            common_rows.append(
                {
                    "cohort_period": cohort,
                    "region": region,
                    "category": category,
                    "horizon_days": horizon,
                    "eligible_customers": len(eligible),
                    "repeat_customers": len(h_repeat),
                    "certain_no_repeat_customers": len(h_no_repeat),
                    "indeterminate_customers": len(h_indeterminate),
                    "repurchase_rate_common_period": _rate(len(h_repeat), decided_denominator),
                    "lower_bound_rate": _rate(len(h_repeat), len(eligible)),
                    "upper_bound_rate": _rate(len(h_repeat) + len(h_indeterminate), len(eligible)),
                }
            )

    return segment_rows, common_rows


def _summary(customers, segments, common_period, impacts, cutoff, horizons) -> dict[str, Any]:
    first_order = [c for c in customers if c.first_order_id]
    mature = [c for c in first_order if c.mature]
    repeat = [c for c in customers if c.repeat_status == "repeat"]
    indeterminate = [c for c in customers if c.repeat_status == "indeterminate"]
    observing = [c for c in customers if c.repeat_status == "observing"]
    return {
        "customers_total": len(customers),
        "member_accounts": sum(len(c.member_ids) for c in customers),
        "first_order_customers": len(first_order),
        "mature_customers": len(mature),
        "window_open_customers": len(first_order) - len(mature),
        "pending_customers": len([c for c in observing if c.repeat_within_window is False]),
        "repeat_customers": len(repeat),
        "no_repeat_customers": len([c for c in customers if c.repeat_status == "no_repeat"]),
        "indeterminate_customers": len(indeterminate),
        "identity_unresolved_customers": len([c for c in customers if c.identity_status != "confirmed"]),
        "impact_count": len(impacts),
        "segment_count": len(segments),
        "horizons": horizons,
        "cutoff": cutoff.date().isoformat(),
        "principle": "Unlinked or disputed customers are reported as indeterminate, never as confirmed non-repurchasers.",
    }


def diff_analysis(before: AnalysisResult | dict[str, Any], after: AnalysisResult | dict[str, Any]) -> dict[str, Any]:
    """Compare two analysis runs and return customer/segment metric changes."""
    old = before if isinstance(before, dict) else before.to_dict()
    new = after if isinstance(after, dict) else after.to_dict()
    old_customers = {c["customer_id"]: c for c in old["customers"]}
    new_customers = {c["customer_id"]: c for c in new["customers"]}
    customer_changes = []
    for cid in sorted(set(old_customers) | set(new_customers)):
        a = old_customers.get(cid)
        b = new_customers.get(cid)
        changed_fields = {}
        for key in [
            "repeat_status",
            "first_order_id",
            "first_order_at",
            "second_order_id",
            "second_order_at",
            "days_to_repeat",
            "mature",
            "observed_days",
            "first_order_net_amount",
            "order_count_valid",
            "identity_status",
            "cohort_period",
            "region",
            "category",
        ]:
            old_value = None if a is None else a.get(key)
            new_value = None if b is None else b.get(key)
            if old_value != new_value:
                changed_fields[key] = {"before": old_value, "after": new_value}
        if changed_fields:
            customer_changes.append({"customer_id": cid, "changes": changed_fields})

    def seg_key(row):
        return row["cohort_period"], row["region"], row["category"]

    old_segments = {seg_key(r): r for r in old["segments"]}
    new_segments = {seg_key(r): r for r in new["segments"]}
    segment_changes = []
    metric_keys = [
        "customers_total",
        "repeat_customers",
        "no_repeat_customers",
        "indeterminate_customers",
        "mature_customers",
        "window_open_customers",
        "repurchase_rate_decided",
        "repurchase_rate_decided_90d",
        "repurchase_rate_lower_bound_90d",
        "repurchase_rate_upper_bound_90d",
    ]
    for key in sorted(set(old_segments) | set(new_segments)):
        changes = {}
        for metric in metric_keys:
            before_value = old_segments.get(key, {}).get(metric)
            after_value = new_segments.get(key, {}).get(metric)
            if before_value != after_value:
                changes[metric] = {"before": before_value, "after": after_value}
        if changes:
            segment_changes.append({"segment": key, "changes": changes})
    return {
        "before_as_of": old.get("as_of"),
        "after_as_of": new.get("as_of"),
        "before_data_hash": old.get("data_hash"),
        "after_data_hash": new.get("data_hash"),
        "customer_changes": customer_changes,
        "segment_changes": segment_changes,
    }
