"""Typed source records and analysis enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class RepurchaseStatus(str, Enum):
    """Customer-level repurchase classification."""

    REPEAT = "repeat"
    NO_REPEAT = "no_repeat"
    OBSERVING = "observing"
    INDETERMINATE = "indeterminate"
    NO_VALID_ORDER = "no_valid_order"


class IdentityStatus(str, Enum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    CONFLICT = "conflict"
    INCOMPLETE = "incomplete"
    REVOKED = "revoked"


@dataclass(frozen=True)
class IdentityLink:
    member: str
    source: str | None
    person: str
    valid_from: date
    valid_to: date | None
    version: int
    state: str
    line: int = 0
    path: str = "identity.csv"


@dataclass(frozen=True)
class ConsentEvent:
    person: str
    channel: str
    state: str
    at: datetime
    line: int = 0
    sequence: int = 0


@dataclass(frozen=True)
class Order:
    order_id: str
    member: str
    channel: str | None
    paid: Decimal
    state: str
    at: datetime
    line: int = 0
    region: str | None = None
    category: str | None = None
    refunded_amount: Decimal | None = None


@dataclass(frozen=True)
class Refund:
    refund_id: str
    order_id: str
    amount: Decimal
    at: datetime
    recorded_at: datetime | None = None
    line: int = 0


@dataclass(frozen=True)
class OrderItem:
    order_id: str
    category: str
    amount: Decimal | None = None
    line: int = 0


@dataclass(frozen=True)
class MemberProfile:
    member: str
    joined_at: date | None = None
    region: str | None = None
    person: str | None = None


@dataclass(frozen=True)
class PersonProfile:
    person: str
    joined_at: date | None = None
    region: str | None = None


@dataclass(frozen=True)
class Exclusion:
    person: str | None
    code: str
    reason: str | None
    valid_from: date | None = None
    valid_to: date | None = None
    member: str | None = None


@dataclass(frozen=True)
class CampaignList:
    list_id: str
    name: str | None
    channel: str
    owner: str
    status: str = "draft"
    frozen_at: datetime | None = None
    sent_at: datetime | None = None
    exclude_recent_days: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ListRecipient:
    list_id: str
    person: str | None = None
    member: str | None = None


@dataclass
class ValidOrder:
    """An order after cancellation/refund corrections have been applied."""

    order: Order
    net_amount: Decimal
    fully_refunded: bool
    refund_total: Decimal
    effective_state: str


@dataclass
class CustomerMetric:
    customer_id: str
    person_id: str | None
    member_ids: list[str]
    cohort_period: str
    joined_at: date | None
    region: str
    category: str
    identity_status: str
    current_identity_status: str = "confirmed"
    identity_flags: list[str] = field(default_factory=list)
    possible_person_ids: list[str] = field(default_factory=list)
    first_order_id: str | None = None
    first_order_at: date | None = None
    first_order_net_amount: str | None = None
    second_order_id: str | None = None
    second_order_at: date | None = None
    days_to_repeat: int | None = None
    observed_days: int = 0
    membership_age_days: int | None = None
    window_days: int = 90
    window_end: date | None = None
    mature: bool = False
    days_remaining: int | None = None
    repeat_status: str = RepurchaseStatus.NO_VALID_ORDER.value
    repeat_within_window: bool | None = None
    identity_material: bool = False
    possible_extra_order_ids: list[str] = field(default_factory=list)
    consent_active_channels: list[str] = field(default_factory=list)
    consent_withdrawn_channels: list[str] = field(default_factory=list)
    order_count_valid: int = 0
    excluded_order_ids: list[str] = field(default_factory=list)
    data_flags: list[str] = field(default_factory=list)

    def segment_key(self) -> tuple[str, str, str]:
        return self.cohort_period, self.region, self.category

    def to_row(self) -> dict[str, Any]:
        row = dict(self.__dict__)
        for key in (
            "member_ids",
            "identity_flags",
            "possible_person_ids",
            "possible_extra_order_ids",
            "consent_active_channels",
            "consent_withdrawn_channels",
            "excluded_order_ids",
            "data_flags",
        ):
            row[key] = ";".join(row[key])
        return row
