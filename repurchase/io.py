"""CSV loading and input-directory validation."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .dates import parse_as_of, parse_date, parse_timestamp
from .errors import DataValidationError
from .models import (
    CampaignList,
    ConsentEvent,
    Exclusion,
    IdentityLink,
    ListRecipient,
    MemberProfile,
    Order,
    OrderItem,
    PersonProfile,
    Refund,
)

REQUIRED_FILES = ("identity.csv", "consent.csv", "orders.csv")


@dataclass
class InputData:
    root: Path
    identity: list[IdentityLink] = field(default_factory=list)
    consent: list[ConsentEvent] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    order_items: list[OrderItem] = field(default_factory=list)
    members: list[MemberProfile] = field(default_factory=list)
    persons: list[PersonProfile] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    campaigns: list[CampaignList] = field(default_factory=list)
    recipients: list[ListRecipient] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)

    def hash_payload(self) -> str:
        """Stable hash of all loaded source hashes."""
        digest = hashlib.sha256()
        for name in sorted(self.hashes):
            digest.update(name.encode())
            digest.update(self.hashes[name].encode())
        return digest.hexdigest()[:16]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv_dicts(path: Path, required: list[str]) -> list[dict[str, str | None]]:
    if not path.exists():
        raise FileNotFoundError(f"required input file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames is None:
            raise DataValidationError(str(path), None, "CSV is empty or missing a header row")
        missing = [name for name in required if name not in reader.fieldnames]
        if missing:
            raise DataValidationError(str(path), 1, f"missing required column(s): {', '.join(missing)}")
        rows: list[dict[str, str | None]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise DataValidationError(str(path), line_number, "row has more columns than the header")
            cleaned: dict[str, str | None] = {}
            for key, value in row.items():
                if key is None:
                    continue
                if value is None or value == "":
                    cleaned[key] = None
                else:
                    cleaned[key] = value.strip()
            cleaned["_line"] = str(line_number)
            rows.append(cleaned)
        return rows


def _text(row: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = row.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _decimal(row: dict[str, Any], key: str, path: Path, required: bool = True) -> Decimal | None:
    raw = row.get(key)
    line = int(row.get("_line", 0))
    if raw is None or raw == "":
        if required:
            raise DataValidationError(str(path), line, f"missing decimal field {key}")
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise DataValidationError(str(path), line, f"invalid decimal {key}={raw!r}") from exc


def _line(row: dict[str, Any]) -> int:
    return int(row.get("_line", 0))


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _text(row, key)
        if value:
            return value
    return None


def _parse_first_date(row: dict[str, Any], keys: tuple[str, ...], path: Path) -> date | None:
    for key in keys:
        value = row.get(key)
        if value:
            return parse_date(value, key, _line(row), str(path))
    return None


def _load_identity(path: Path) -> list[IdentityLink]:
    rows = read_csv_dicts(path, ["member", "person", "from", "version", "state"])
    result = []
    for row in rows:
        line = _line(row)
        valid_from = parse_date(row.get("from"), "from", line, str(path))
        if valid_from is None:
            raise DataValidationError(str(path), line, "missing required from date")
        try:
            version = int(_text(row, "version", "1"))  # type: ignore[arg-type]
        except ValueError as exc:
            raise DataValidationError(str(path), line, "version must be an integer") from exc
        result.append(
            IdentityLink(
                member=_text(row, "member", "") or "",
                source=_text(row, "source"),
                person=_text(row, "person", "") or "",
                valid_from=valid_from,
                valid_to=parse_date(row.get("to"), "to", line, str(path)),
                version=version,
                state=(_text(row, "state", "unconfirmed") or "unconfirmed").lower(),
                line=line,
                path=path.name,
            )
        )
    if any(not item.member or not item.person for item in result):
        raise DataValidationError(str(path), None, "member and person are required on every identity row")
    return result


def _load_consent(path: Path) -> list[ConsentEvent]:
    rows = read_csv_dicts(path, ["person", "channel", "state", "at"])
    result = []
    for index, row in enumerate(rows):
        line = _line(row)
        at = parse_timestamp(row.get("at"), "at", line, str(path))
        if at is None:
            raise DataValidationError(str(path), line, "missing consent event timestamp")
        result.append(
            ConsentEvent(
                person=_text(row, "person", "") or "",
                channel=(_text(row, "channel", "") or "").lower(),
                state=(_text(row, "state", "") or "").lower(),
                at=at,
                line=line,
                sequence=index,
            )
        )
    return result


def _load_orders(path: Path) -> list[Order]:
    rows = read_csv_dicts(path, ["order_id", "member", "paid", "state", "at"])
    result = []
    seen: set[str] = set()
    for row in rows:
        line = _line(row)
        order_id = _text(row, "order_id", "") or ""
        if order_id in seen:
            raise DataValidationError(str(path), line, f"duplicate order_id {order_id!r}; put corrections in refunds/order updates or version the source")
        seen.add(order_id)
        at = parse_timestamp(row.get("at"), "at", line, str(path))
        if at is None:
            raise DataValidationError(str(path), line, "missing order timestamp")
        result.append(
            Order(
                order_id=order_id,
                member=_text(row, "member", "") or "",
                channel=(_text(row, "channel") or None),
                paid=_decimal(row, "paid", path) or Decimal("0"),
                state=(_text(row, "state", "") or "").lower(),
                at=at,
                line=line,
                region=_first_text(row, "region", "area", "地区"),
                category=_first_text(row, "category", "category_name", "品类"),
                refunded_amount=_decimal(row, "refunded_amount", path, required=False),
            )
        )
    return result


def _load_refunds(path: Path) -> list[Refund]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["refund_id", "order_id", "amount", "at"])
    result = []
    seen: set[str] = set()
    for row in rows:
        refund_id = _text(row, "refund_id", "") or ""
        if refund_id in seen:
            raise DataValidationError(str(path), _line(row), f"duplicate refund_id {refund_id!r}")
        seen.add(refund_id)
        at = parse_timestamp(row.get("at"), "at", _line(row), str(path))
        if at is None:
            raise DataValidationError(str(path), _line(row), "missing refund timestamp")
        result.append(
            Refund(
                refund_id=refund_id,
                order_id=_text(row, "order_id", "") or "",
                amount=_decimal(row, "amount", path) or Decimal("0"),
                at=at,
                recorded_at=parse_timestamp(row.get("recorded_at"), "recorded_at", _line(row), str(path)),
                line=_line(row),
            )
        )
    return result


def _load_order_items(path: Path) -> list[OrderItem]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["order_id", "category"])
    return [
        OrderItem(
            order_id=_text(row, "order_id", "") or "",
            category=_text(row, "category", "") or "未分类",
            amount=_decimal(row, "amount", path, required=False),
            line=_line(row),
        )
        for row in rows
    ]


def _load_members(path: Path) -> list[MemberProfile]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["member"])
    return [
        MemberProfile(
            member=_text(row, "member", "") or "",
            joined_at=_parse_first_date(
                row,
                ("joined_at", "join_date", "joined", "入会日期", "入会时间"),
                path,
            ),
            region=_first_text(row, "region", "area", "地区"),
            person=_text(row, "person"),
        )
        for row in rows
    ]


def _load_persons(path: Path) -> list[PersonProfile]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["person"])
    return [
        PersonProfile(
            person=_text(row, "person", "") or "",
            joined_at=_parse_first_date(
                row,
                ("joined_at", "join_date", "joined", "入会日期", "入会时间"),
                path,
            ),
            region=_first_text(row, "region", "area", "地区"),
        )
        for row in rows
    ]


def _load_exclusions(path: Path) -> list[Exclusion]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["code"])
    result = []
    for row in rows:
        person = _text(row, "person")
        member = _text(row, "member")
        if not person and not member:
            raise DataValidationError(str(path), _line(row), "exclusion requires person or member")
        result.append(
            Exclusion(
                person=person,
                member=member,
                code=_text(row, "code", "") or "excluded",
                reason=_text(row, "reason"),
                valid_from=parse_date(row.get("from"), "from", _line(row), str(path)),
                valid_to=parse_date(row.get("to"), "to", _line(row), str(path)),
            )
        )
    return result


def _optional_int(row: dict[str, str | None], key: str) -> int | None:
    raw = row.get(key)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw))
    except ValueError as exc:
        raise DataValidationError("campaign_lists.csv", _line(row), f"{key} must be an integer") from exc


def _load_campaigns(path: Path) -> list[CampaignList]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["list_id", "channel", "owner"])
    result = []
    for row in rows:
        frozen_raw = row.get("frozen_at")
        sent_raw = row.get("sent_at")
        frozen_at = parse_as_of(frozen_raw) if frozen_raw else None
        sent_at = parse_as_of(sent_raw) if sent_raw else None
        result.append(
            CampaignList(
                list_id=_text(row, "list_id", "") or "",
                name=_text(row, "name"),
                channel=(_text(row, "channel", "") or "").lower(),
                owner=_text(row, "owner", "") or "unassigned",
                status=(_text(row, "status", "draft") or "draft").lower(),
                frozen_at=frozen_at,
                sent_at=sent_at,
                exclude_recent_days=_optional_int(row, "exclude_recent_days"),
                notes=_text(row, "notes"),
            )
        )
    return result


def _load_recipients(path: Path) -> list[ListRecipient]:
    if not path.exists():
        return []
    rows = read_csv_dicts(path, ["list_id"])
    result = []
    for row in rows:
        person = _text(row, "person")
        member = _text(row, "member")
        if not person and not member:
            raise DataValidationError(str(path), _line(row), "recipient requires person or member")
        result.append(ListRecipient(list_id=_text(row, "list_id", "") or "", person=person, member=member))
    return result


def load_input(root: str | Path) -> InputData:
    """Load a directory of CSV inputs. Optional governance files are detected."""
    base = Path(root)
    missing = [name for name in REQUIRED_FILES if not (base / name).exists()]
    if missing:
        raise FileNotFoundError(f"input directory {base} is missing: {', '.join(missing)}")

    data = InputData(root=base)
    loaders = {
        "identity.csv": (_load_identity, "identity"),
        "consent.csv": (_load_consent, "consent"),
        "orders.csv": (_load_orders, "orders"),
        "refunds.csv": (_load_refunds, "refunds"),
        "order_items.csv": (_load_order_items, "order_items"),
        "members.csv": (_load_members, "members"),
        "persons.csv": (_load_persons, "persons"),
        "exclusions.csv": (_load_exclusions, "exclusions"),
        "campaign_lists.csv": (_load_campaigns, "campaigns"),
        "campaign_recipients.csv": (_load_recipients, "recipients"),
    }
    for filename, (loader, attr) in loaders.items():
        path = base / filename
        if path.exists():
            setattr(data, attr, loader(path))
            data.hashes[filename] = _file_hash(path)

    order_ids = {order.order_id for order in data.orders}
    for refund in data.refunds:
        if refund.order_id not in order_ids:
            raise DataValidationError(str(base / "refunds.csv"), refund.line, f"refund references unknown order {refund.order_id}")
    item_order_ids = {item.order_id for item in data.order_items}
    orphan_items = item_order_ids - order_ids
    if orphan_items:
        raise DataValidationError(str(base / "order_items.csv"), None, f"order items reference unknown orders: {sorted(orphan_items)}")
    return data
