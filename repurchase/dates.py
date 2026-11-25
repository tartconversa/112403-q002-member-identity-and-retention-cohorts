"""Date and period helpers.

Date-only source values are treated as calendar days.  Timestamps are compared
as naive values after normalizing aware values to UTC; the synthetic files in
this project do not require timezone-aware wall-clock semantics.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .errors import DataValidationError


def parse_date(value: object, field: str = "date", line: int | None = None, path: str = "<input>") -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise DataValidationError(path, line, f"invalid {field} {text!r}: {exc}") from exc


def parse_timestamp(
    value: object,
    field: str = "timestamp",
    line: int | None = None,
    path: str = "<input>",
) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time.min)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise DataValidationError(path, line, f"invalid {field} {text!r}: {exc}") from exc


def parse_as_of(value: str | datetime | date | None) -> datetime:
    if value is None:
        # Deterministic fallback used only when the caller cannot provide a
        # cutoff.  The CLI defaults to the maximum source timestamp instead.
        return datetime.combine(date.today(), time.max)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.max)
    parsed = parse_timestamp(value, "as_of")
    assert parsed is not None
    if len(value.strip()) == 10:
        return datetime.combine(parsed.date(), time.max)
    return parsed


def day_number(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def elapsed_days(start: datetime | date, end: datetime | date) -> int:
    """Calendar-day difference.

    For date-only inputs this is the usual ``(end_date - start_date).days``.
    For timestamps, the value is floored to completed 24-hour periods?  The
    business window is expressed in days and source data is day-granular, so we
    compare calendar dates to avoid a late-afternoon timestamp making a day-90
    purchase appear to be day 91.
    """
    return (day_number(end) - day_number(start)).days


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def month_period(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def quarter_period(value: date) -> str:
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year:04d}-Q{quarter}"


def cohort_period(value: date | None, grain: str = "month") -> str:
    if value is None:
        return "未知入会期"
    if grain == "month":
        return month_period(value)
    if grain == "quarter":
        return quarter_period(value)
    if grain == "date":
        return value.isoformat()
    raise ValueError(f"unsupported cohort grain: {grain}")


def median_int(values: Iterable[int]) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2
