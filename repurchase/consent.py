"""Point-in-time marketing consent rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .models import ConsentEvent

ACTIVE_STATES = {
    "active", "consented", "opt_in", "opted_in", "subscribed", "yes", "true", "1",
    "有效", "已授权", "同意", "已同意",
}
WITHDRAWN_STATES = {
    "withdrawn", "withdraw", "revoked", "opt_out", "opted_out", "unsubscribed", "no", "false", "0",
    "撤回", "已撤回", "撤销", "已撤销", "退订", "拒绝",
}


def is_active_state(state: str | None) -> bool:
    return (state or "").lower() in ACTIVE_STATES


@dataclass
class ConsentDecision:
    person: str
    channel: str
    state: str  # active | withdrawn | unknown
    effective_event: ConsentEvent | None

    @property
    def allowed(self) -> bool:
        return self.state == "active"


class ConsentLedger:
    def __init__(self, events: Iterable[ConsentEvent]):
        self.events = list(events)
        self._by_key: dict[tuple[str, str], list[ConsentEvent]] = {}
        for event in self.events:
            self._by_key.setdefault((event.person, event.channel.lower()), []).append(event)
        for items in self._by_key.values():
            # Timestamp, then source order for equal-timestamp corrections.
            items.sort(key=lambda e: (e.at, e.sequence))

    def decision(self, person: str, channel: str, at: datetime) -> ConsentDecision:
        history = self._by_key.get((person, channel.lower()), [])
        effective: ConsentEvent | None = None
        for event in history:
            if event.at <= at:
                effective = event
            else:
                break
        if effective is None:
            return ConsentDecision(person, channel.lower(), "unknown", None)
        state = effective.state.lower()
        if state in ACTIVE_STATES:
            canonical = "active"
        elif state in WITHDRAWN_STATES:
            canonical = "withdrawn"
        else:
            # Unknown consent values must not be interpreted as opt-in.
            canonical = "unknown"
        return ConsentDecision(person, channel.lower(), canonical, effective)

    def current_channels(self, person: str, at: datetime) -> tuple[list[str], list[str]]:
        active: list[str] = []
        withdrawn: list[str] = []
        for (known_person, channel), _events in self._by_key.items():
            if known_person != person:
                continue
            decision = self.decision(person, channel, at)
            if decision.state == "active":
                active.append(channel)
            elif decision.state == "withdrawn":
                withdrawn.append(channel)
        return sorted(active), sorted(withdrawn)

    def withdrawals(self, at: datetime | None = None) -> list[ConsentEvent]:
        cutoff = at or datetime.max
        return [e for e in self.events if e.at <= cutoff and e.state.lower() in WITHDRAWN_STATES]
