"""Campaign freezes, immutable sent records, and reverification."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .consent import ConsentLedger
from .dates import day_number, parse_as_of
from .errors import CampaignStateError
from .identity import IdentityGraph
from .io import InputData
from .models import CampaignList, ListRecipient
from .orders import build_valid_orders, is_effectively_valid

SNAPSHOT_VERSION = 1
SENT_STATUSES = {"sent", "locked", "已发送", "已锁定"}


def _exclusion_active(exclusion, at: datetime) -> bool:
    day = day_number(at)
    if exclusion.valid_from and exclusion.valid_from > day:
        return False
    if exclusion.valid_to and exclusion.valid_to < day:
        return False
    return True


def evaluate_recipient(
    data: InputData,
    campaign: CampaignList,
    recipient: ListRecipient,
    at: datetime | str | None = None,
    graph: IdentityGraph | None = None,
) -> dict[str, Any]:
    """Evaluate one recipient against rules effective at the freeze instant."""
    when = parse_as_of(at)
    graph = graph or IdentityGraph(data.identity)
    consent = ConsentLedger(data.consent)
    reasons: list[str] = []
    warnings: list[str] = []

    target_person = recipient.person or None
    target_member = recipient.member or None
    resolved_person: str | None = target_person
    identity_status = "confirmed"

    member_resolution = None
    if target_member:
        member_resolution = graph.resolve(target_member, when)
        identity_status = member_resolution.status
        if member_resolution.person:
            if target_person and target_person != member_resolution.person:
                reasons.append("identity_person_mismatch")
            resolved_person = member_resolution.person
        else:
            resolved_person = target_person  # Do not infer from a disputed person.
        if identity_status != "confirmed":
            reasons.append(f"identity_{identity_status}")

    if target_person:
        person_identity = graph.person_identity_status(target_person, when)
        # A member-specific resolution can be worse/more precise than the
        # person-wide check.
        if member_resolution is None or person_identity.status != "confirmed":
            if person_identity.status != "confirmed":
                identity_status = person_identity.status
                if f"identity_{person_identity.status}" not in reasons:
                    reasons.append(f"identity_{person_identity.status}")

    consent_state = "unknown"
    effective_consent_at = None
    if resolved_person:
        decision = consent.decision(resolved_person, campaign.channel, when)
        consent_state = decision.state
        effective_consent_at = decision.effective_event.at.isoformat() if decision.effective_event else None
        if not decision.allowed:
            reasons.append(f"consent_{decision.state}")
    else:
        reasons.append("consent_unknown")

    active_exclusion_codes: list[str] = []
    resolved_member_ids = {target_member} if target_member else set()
    if resolved_person:
        for link in graph.by_person.get(resolved_person, []):
            member_resolution = graph.resolve(link.member, when)
            if member_resolution.person == resolved_person:
                resolved_member_ids.add(link.member)
    for exclusion in data.exclusions:
        if not _exclusion_active(exclusion, when):
            continue
        matches_person = resolved_person and exclusion.person == resolved_person
        matches_target_person = target_person and exclusion.person == target_person
        matches_member = exclusion.member and exclusion.member in resolved_member_ids
        if matches_person or matches_target_person or matches_member:
            active_exclusion_codes.append(exclusion.code)
            reasons.append(f"exclusion_{exclusion.code}")

    recent_order_ids: list[str] = []
    if campaign.exclude_recent_days is not None and campaign.exclude_recent_days >= 0:
        refunds = [r for r in data.refunds if r.at <= when]
        valid_orders = build_valid_orders(data.orders, refunds)
        for order in data.orders:
            if order.at > when:
                continue
            record = valid_orders[order.order_id]
            if not is_effectively_valid(record):
                continue
            belongs = order.member == target_member
            if resolved_person:
                resolution = graph.resolve(order.member, order.at)
                belongs = belongs or resolution.person == resolved_person
            if belongs and (when.date() - order.at.date()).days <= campaign.exclude_recent_days:
                recent_order_ids.append(order.order_id)
        if recent_order_ids:
            reasons.append("recent_valid_order")

    eligible = not reasons
    return {
        "target_person": target_person,
        "target_member": target_member,
        "resolved_person": resolved_person,
        "channel": campaign.channel,
        "evaluated_at": when.isoformat(timespec="seconds"),
        "eligible": eligible,
        "reasons": reasons,
        "warnings": warnings,
        "consent_state": consent_state,
        "effective_consent_at": effective_consent_at,
        "identity_status": identity_status,
        "active_exclusion_codes": sorted(set(active_exclusion_codes)),
        "recent_order_ids": sorted(set(recent_order_ids)),
    }


class CampaignService:
    """File-backed freeze/send/review workflow.

    ``state_dir`` contains mutable pointers and alert reports.  Files under
    ``sent/`` are write-once official records; this class never edits them.
    """

    def __init__(self, data: InputData, state_dir: str | Path):
        self.data = data
        self.state_dir = Path(state_dir)
        self.snapshot_dir = self.state_dir / "snapshots"
        self.sent_dir = self.state_dir / "sent"
        self.alerts_dir = self.state_dir / "alerts"
        for path in (self.snapshot_dir, self.sent_dir, self.alerts_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"version": 1, "lists": {}}

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _campaign(self, list_id: str) -> CampaignList:
        for campaign in self.data.campaigns:
            if campaign.list_id == list_id:
                return campaign
        raise KeyError(f"unknown campaign list {list_id!r}; add it to campaign_lists.csv")

    def _recipients(self, list_id: str) -> list[ListRecipient]:
        rows = [r for r in self.data.recipients if r.list_id == list_id]
        if not rows:
            raise KeyError(f"campaign list {list_id!r} has no recipients in campaign_recipients.csv")
        return rows

    def _snapshot_path(self, list_id: str, when: datetime, kind: str) -> Path:
        stamp = when.strftime("%Y%m%dT%H%M%S%f")
        return self.snapshot_dir / f"{list_id}__{stamp}__{kind}.json"

    def freeze_list(
        self,
        list_id: str,
        at: str | datetime | None = None,
        *,
        kind: str = "freeze",
        recipients: Iterable[ListRecipient] | None = None,
    ) -> dict[str, Any]:
        campaign = self._campaign(list_id)
        when = parse_as_of(at or campaign.frozen_at)
        sent_path = self.sent_dir / f"{list_id}.sent.json"
        if sent_path.exists():
            raise CampaignStateError(f"{list_id} has already been sent; official sent record is immutable: {sent_path}")
        if campaign.status in SENT_STATUSES and kind != "reverification":
            raise CampaignStateError(f"{list_id} is marked sent in campaign_lists.csv; refusing to replace sent records")

        recipient_rows = list(recipients) if recipients is not None else self._recipients(list_id)
        graph = IdentityGraph(self.data.identity)
        evaluated = [evaluate_recipient(self.data, campaign, recipient, when, graph) for recipient in recipient_rows]
        snapshot = {
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot_id": f"{list_id}-{when.strftime('%Y%m%dT%H%M%S%f')}-{kind}",
            "kind": kind,
            "list_id": list_id,
            "name": campaign.name,
            "channel": campaign.channel,
            "owner": campaign.owner,
            "frozen_at": when.isoformat(timespec="seconds"),
            "data_hash": self.data.hash_payload(),
            "campaign": asdict(campaign) | {"frozen_at": campaign.frozen_at.isoformat() if campaign.frozen_at else None, "sent_at": campaign.sent_at.isoformat() if campaign.sent_at else None},
            "summary": {
                "recipients": len(evaluated),
                "eligible": sum(1 for row in evaluated if row["eligible"]),
                "excluded": sum(1 for row in evaluated if not row["eligible"]),
            },
            "rows": evaluated,
        }
        path = self._snapshot_path(list_id, when, kind)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        list_state = self.state["lists"].setdefault(list_id, {})
        list_state.update(
            {
                "owner": campaign.owner,
                "channel": campaign.channel,
                "status": "frozen",
                "current_snapshot": str(path),
                "last_frozen_at": snapshot["frozen_at"],
            }
        )
        self._save_state()
        self._write_snapshot_csv(path.with_suffix(".csv"), snapshot)
        return snapshot

    @staticmethod
    def _write_snapshot_csv(path: Path, snapshot: dict[str, Any]) -> None:
        fields = [
            "target_person",
            "target_member",
            "resolved_person",
            "eligible",
            "reasons",
            "consent_state",
            "identity_status",
            "evaluated_at",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in snapshot["rows"]:
                output = {key: row.get(key) for key in fields}
                output["eligible"] = "yes" if row["eligible"] else "no"
                output["reasons"] = ";".join(row["reasons"])
                writer.writerow(output)

    def mark_sent(self, list_id: str, at: str | datetime | None = None) -> dict[str, Any]:
        list_state = self.state["lists"].get(list_id)
        if not list_state or not list_state.get("current_snapshot"):
            raise CampaignStateError(f"freeze {list_id} before marking it sent")
        sent_path = self.sent_dir / f"{list_id}.sent.json"
        if sent_path.exists():
            raise CampaignStateError(f"sent record already exists and cannot be rewritten: {sent_path}")
        snapshot_path = Path(list_state["current_snapshot"])
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        when = parse_as_of(at)
        sent_record = {
            **snapshot,
            "sent_at": when.isoformat(timespec="seconds"),
            "sent_record_locked": True,
        }
        sent_path.write_text(json.dumps(sent_record, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        list_state["status"] = "sent"
        list_state["sent_record"] = str(sent_path)
        list_state["sent_at"] = sent_record["sent_at"]
        self._save_state()
        return sent_record

    def review_lists(self, at: str | datetime | None = None, analysis_result=None) -> dict[str, Any]:
        when = parse_as_of(at)
        report: dict[str, Any] = {"reviewed_at": when.isoformat(timespec="seconds"), "lists": [], "alerts": []}

        # Impact subjects from the current analysis help explain *why* a
        # correction matters even when final eligibility happens not to change.
        impact_by_subject: dict[str, list[dict[str, Any]]] = {}
        if analysis_result is not None:
            result_dict = analysis_result.to_dict() if hasattr(analysis_result, "to_dict") else analysis_result
            for impact in result_dict.get("impacts", []):
                subjects = {impact.get("subject"), impact.get("customer_id")}
                subjects.update(impact.get("member_ids", []) or [])
                subjects.update(impact.get("possible_person_ids", []) or [])
                for subject in subjects:
                    if subject:
                        impact_by_subject.setdefault(subject, []).append(impact)

        configured_ids = {campaign.list_id for campaign in self.data.campaigns}
        all_list_ids = sorted(set(self.state["lists"]) | configured_ids)
        for list_id in all_list_ids:
            list_state = self.state["lists"].get(list_id, {})
            sent_path = self.sent_dir / f"{list_id}.sent.json"
            campaign = self._campaign_if_present(list_id)
            if sent_path.exists():
                sent_record = json.loads(sent_path.read_text(encoding="utf-8"))
                current_rows = []
                for sent_row in sent_record["rows"]:
                    recipient = ListRecipient(
                        list_id=list_id,
                        person=sent_row.get("target_person"),
                        member=sent_row.get("target_member"),
                    )
                    current = evaluate_recipient(self.data, campaign or self._campaign_from_snapshot(sent_record), recipient, when)
                    current_rows.append({"sent": sent_row, "current": current})
                    changed = sent_row.get("eligible") != current.get("eligible") or sent_row.get("reasons") != current.get("reasons")
                    related_impacts = self._related_impacts(sent_row, impact_by_subject)
                    if changed or related_impacts:
                        alert = {
                            "list_id": list_id,
                            "owner": sent_record.get("owner"),
                            "channel": sent_record.get("channel"),
                            "record_type": "sent_immutable",
                            "sent_at": sent_record.get("sent_at"),
                            "target_person": sent_row.get("target_person"),
                            "target_member": sent_row.get("target_member"),
                            "sent_eligible": sent_row.get("eligible"),
                            "current_eligible": current.get("eligible"),
                            "sent_reasons": sent_row.get("reasons"),
                            "current_reasons": current.get("reasons"),
                            "eligibility_changed": sent_row.get("eligible") != current.get("eligible"),
                            "related_impacts": related_impacts,
                            "required_action": "Notify owner; do not rewrite or recall the sent record. Suppress the recipient in future sends if now ineligible.",
                        }
                        report["alerts"].append(alert)
                report["lists"].append({"list_id": list_id, "status": "sent", "sent_record": str(sent_path), "checked": len(current_rows)})
            else:
                # Capture the old pointer before freeze_list() updates state.
                old_path = list_state.get("current_snapshot")
                old = json.loads(Path(old_path).read_text(encoding="utf-8")) if old_path and Path(old_path).exists() else None
                try:
                    new_snapshot = self.freeze_list(list_id, when, kind="reverification")
                    current_path = self.state["lists"][list_id]["current_snapshot"]
                    changes = self._snapshot_changes(old, new_snapshot) if old else []
                    change_keys = {(c.get("target_person"), c.get("target_member")) for c in changes}
                    for change in changes:
                        target_row = {
                            "target_person": change.get("target_person"),
                            "target_member": change.get("target_member"),
                            "resolved_person": change.get("resolved_person"),
                        }
                        change["related_impacts"] = self._related_impacts(target_row, impact_by_subject)
                        report["alerts"].append(
                            {
                                "list_id": list_id,
                                "owner": new_snapshot["owner"],
                                "channel": new_snapshot.get("channel"),
                                "record_type": "unsent_reverification",
                                "current_snapshot": current_path,
                                **change,
                                "required_action": "Use the new reverified snapshot before sending.",
                            }
                        )

                    # A correction can matter even if eligibility is unchanged.
                    for current_row in new_snapshot["rows"]:
                        key = (current_row.get("target_person"), current_row.get("target_member"))
                        if key in change_keys:
                            continue
                        related = self._related_impacts(current_row, impact_by_subject)
                        if related:
                            report["alerts"].append(
                                {
                                    "list_id": list_id,
                                    "owner": new_snapshot["owner"],
                                    "channel": new_snapshot.get("channel"),
                                    "record_type": "unsent_data_correction",
                                    "target_person": current_row.get("target_person"),
                                    "target_member": current_row.get("target_member"),
                                    "current_eligible": current_row.get("eligible"),
                                    "current_reasons": current_row.get("reasons"),
                                    "eligibility_changed": False,
                                    "related_impacts": related,
                                    "required_action": "Review the correction; the unsent snapshot has been reverified.",
                                }
                            )

                    report["lists"].append(
                        {
                            "list_id": list_id,
                            "status": "unsent_reverified",
                            "current_snapshot": current_path,
                            "changes": changes,
                            "eligible": new_snapshot["summary"]["eligible"],
                            "excluded": new_snapshot["summary"]["excluded"],
                        }
                    )
                except KeyError as exc:
                    report["lists"].append({"list_id": list_id, "status": "missing_roster_or_definition", "error": str(exc)})

        self._attach_impact_lists(analysis_result, report)
        self._write_reports(report)
        return report

    @staticmethod
    def _attach_impact_lists(analysis_result: Any, report: dict[str, Any]) -> None:
        if analysis_result is None:
            return
        if hasattr(analysis_result, "impacts"):
            impacts = analysis_result.impacts
        elif isinstance(analysis_result, dict):
            impacts = analysis_result.get("impacts", [])
        else:
            return
        for impact in impacts:
            impact_id = impact.get("impact_id")
            related_alerts = [
                alert
                for alert in report.get("alerts", [])
                if any(related.get("impact_id") == impact_id for related in alert.get("related_impacts", []))
            ]
            if related_alerts:
                impact["affected_list_ids"] = sorted({alert["list_id"] for alert in related_alerts})
                impact["owners"] = sorted({alert.get("owner") or "unassigned" for alert in related_alerts})

    def _campaign_if_present(self, list_id: str) -> CampaignList | None:
        return next((c for c in self.data.campaigns if c.list_id == list_id), None)

    @staticmethod
    def _campaign_from_snapshot(snapshot: dict[str, Any]) -> CampaignList:
        raw = snapshot.get("campaign", {})
        return CampaignList(
            list_id=snapshot["list_id"],
            name=snapshot.get("name"),
            channel=snapshot["channel"],
            owner=snapshot.get("owner", "unassigned"),
            status=raw.get("status", "frozen"),
            exclude_recent_days=raw.get("exclude_recent_days"),
        )

    @staticmethod
    def _related_impacts(sent_row: dict[str, Any], impact_by_subject: dict[str, list[dict]]) -> list[dict]:
        values = {sent_row.get("target_person"), sent_row.get("target_member"), sent_row.get("resolved_person")}
        impacts = []
        seen = set()
        for value in values:
            for impact in impact_by_subject.get(value or "", []):
                impact_id = impact.get("impact_id")
                if impact_id not in seen:
                    seen.add(impact_id)
                    impacts.append(impact)
        return impacts

    @staticmethod
    def _snapshot_changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
        if not old:
            return []
        old_rows = {
            (r.get("target_person"), r.get("target_member")): r for r in old.get("rows", [])
        }
        new_rows = {
            (r.get("target_person"), r.get("target_member")): r for r in new.get("rows", [])
        }
        changes = []
        for key, row in new_rows.items():
            person, member = key
            before = old_rows.get(key)
            if before is None:
                changes.append(
                    {
                        "change_type": "added_to_roster",
                        "target_person": person,
                        "target_member": member,
                        "current_eligible": row.get("eligible"),
                        "current_reasons": row.get("reasons"),
                        "resolved_person": row.get("resolved_person"),
                    }
                )
            elif before.get("eligible") != row.get("eligible") or before.get("reasons") != row.get("reasons"):
                changes.append(
                    {
                        "change_type": "eligibility_changed",
                        "target_person": person,
                        "target_member": member,
                        "resolved_person": row.get("resolved_person"),
                        "eligibility_changed": True,
                        "before_eligible": before.get("eligible"),
                        "sent_eligible": before.get("eligible"),
                        "after_eligible": row.get("eligible"),
                        "current_eligible": row.get("eligible"),
                        "before_reasons": before.get("reasons"),
                        "sent_reasons": before.get("reasons"),
                        "after_reasons": row.get("reasons"),
                        "current_reasons": row.get("reasons"),
                    }
                )
        for key, before in old_rows.items():
            if key not in new_rows:
                person, member = key
                changes.append(
                    {
                        "change_type": "removed_from_roster",
                        "target_person": person,
                        "target_member": member,
                        "before_eligible": before.get("eligible"),
                        "sent_eligible": before.get("eligible"),
                        "eligibility_changed": False,
                    }
                )
        return changes

    def _write_reports(self, report: dict[str, Any]) -> None:
        json_path = self.alerts_dir / "list_review.json"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        csv_path = self.alerts_dir / "list_alerts.csv"
        fields = [
            "list_id", "owner", "record_type", "change_type",
            "target_person", "target_member", "eligibility_changed",
            "sent_eligible", "current_eligible", "current_reasons",
            "required_action",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for alert in report["alerts"]:
                row = dict(alert)
                if isinstance(row.get("current_reasons"), list):
                    row["current_reasons"] = ";".join(row["current_reasons"])
                writer.writerow(row)

        by_owner: dict[str, list[dict]] = {}
        for alert in report["alerts"]:
            by_owner.setdefault(alert.get("owner") or "unassigned", []).append(alert)
        for owner, alerts in by_owner.items():
            safe_owner = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in owner)
            path = self.alerts_dir / f"owner_{safe_owner}.md"
            lines = [f"# 名单负责人提醒：{owner}", "", f"核验时间：{report['reviewed_at']}", ""]
            for alert in alerts:
                title = f"- **{alert['list_id']}** ({alert['record_type']}"
                if alert.get("change_type"):
                    title += f"/{alert['change_type']}"
                lines.append(title + ")")
                if alert.get("target_person"):
                    lines.append(f"  - person: `{alert['target_person']}`")
                if alert.get("target_member"):
                    lines.append(f"  - member: `{alert['target_member']}`")
                if "sent_eligible" in alert:
                    lines.append(f"  - 冻结/发送时资格：{alert['sent_eligible']}；当前资格：{alert.get('current_eligible')}")
                elif "current_eligible" in alert:
                    lines.append(f"  - 当前资格：{alert.get('current_eligible')}")
                if alert.get("current_reasons"):
                    lines.append(f"  - 当前原因：{', '.join(alert['current_reasons'])}")
                if alert.get("required_action"):
                    lines.append(f"  - 处理：{alert['required_action']}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
