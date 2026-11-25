"""Versioned, validity-bounded identity resolution.

Only ``confirmed`` assertions are used to merge member IDs.  Disputed and
unknown assertions are retained as *possible* edges for impact analysis but are
never silently merged.  Revoked assertions are excluded from the current
possible graph; their correction history is still reported by callers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .dates import day_number
from .models import IdentityLink

CONFIRMED_STATES = {
    "confirmed", "active", "approved", "merged", "valid",
    "已确认", "确认", "有效", "已合并",
}
DISPUTED_STATES = {
    "disputed", "pending", "review", "uncertain", "suspected", "open",
    "争议", "存疑", "待审核", "待确认", "疑似",
}
REVOKED_STATES = {
    "revoked", "unlinked", "rejected", "inactive", "closed", "invalid",
    "已撤销", "撤销", "已解除", "已拒绝", "失效", "无效",
}


def canonical_identity_state(state: str | None) -> str:
    value = (state or "").lower()
    if value in CONFIRMED_STATES:
        return "confirmed"
    if value in DISPUTED_STATES:
        return "disputed"
    if value in REVOKED_STATES:
        return "revoked"
    return "unknown"


@dataclass
class IdentityResolution:
    member: str
    at: datetime
    status: str  # confirmed | disputed | conflict | revoked | incomplete
    person: str | None = None
    possible_persons: list[str] = field(default_factory=list)
    links: list[IdentityLink] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.status == "confirmed" and self.person is not None


class IdentityGraph:
    def __init__(self, links: list[IdentityLink]):
        self.links = links
        self.by_member: dict[str, list[IdentityLink]] = defaultdict(list)
        self.by_person: dict[str, list[IdentityLink]] = defaultdict(list)
        for link in links:
            self.by_member[link.member].append(link)
            self.by_person[link.person].append(link)
        self._global_governing = self._latest_by_key(links)

    @staticmethod
    def _key(link: IdentityLink) -> tuple[str, str]:
        # Versions describe corrections to one member-to-person relationship.
        # Source is retained for provenance but does not split version history
        # for the same governed relationship.
        return link.member, link.person

    def _latest_by_key(self, links: list[IdentityLink]) -> dict[tuple[str, str], IdentityLink]:
        best: dict[tuple[str, str, str], IdentityLink] = {}
        for link in links:
            key = self._key(link)
            current = best.get(key)
            if current is None or link.version > current.version or (
                link.version == current.version and link.line > current.line
            ):
                best[key] = link
        return best

    def covering(self, member: str, at: datetime) -> list[IdentityLink]:
        day = day_number(at)
        result = []
        for link in self.by_member.get(member, []):
            if link.valid_from <= day and (link.valid_to is None or day <= link.valid_to):
                result.append(link)
        return result

    def governing_links_at(self, member: str, at: datetime) -> list[IdentityLink]:
        return list(self._latest_by_key(self.covering(member, at)).values())

    def resolve(self, member: str, at: datetime) -> IdentityResolution:
        governing = self.governing_links_at(member, at)
        if not governing:
            flags: list[str] = []
            status = "incomplete"
            if member in self.by_member:
                flags.append("identity_link_not_valid_at_time")
                # Distinguish an expired interval from a member that was never
                # present in the identity governance extract.
                if all(link.valid_to is not None and link.valid_to < day_number(at) for link in self.by_member[member]):
                    status = "revoked"
                    flags.append("identity_link_expired_or_revoked")
            else:
                flags.append("member_missing_from_identity_registry")
            return IdentityResolution(member=member, at=at, status=status, flags=flags)

        confirmed = {link.person for link in governing if canonical_identity_state(link.state) == "confirmed"}
        disputed = sorted(
            {link.person for link in governing if canonical_identity_state(link.state) in {"disputed", "unknown"}}
        )
        revoked = [link for link in governing if canonical_identity_state(link.state) == "revoked"]
        flags = sorted({f"identity_state_{canonical_identity_state(link.state)}" for link in governing})

        if len(confirmed) > 1:
            persons = sorted(confirmed | set(disputed))
            return IdentityResolution(
                member=member,
                at=at,
                status="conflict",
                person=None,
                possible_persons=persons,
                links=governing,
                flags=flags + ["multiple_conflicting_confirmed_persons"],
            )
        if len(confirmed) == 1:
            person = next(iter(confirmed))
            status = "confirmed"
            if disputed:
                status = "disputed"
                flags.append("trusted_link_with_disputed_alternative")
            return IdentityResolution(
                member=member,
                at=at,
                status=status,
                person=person,
                possible_persons=sorted(set(disputed) - {person}),
                links=governing,
                flags=flags,
            )
        if disputed:
            return IdentityResolution(
                member=member,
                at=at,
                status="disputed",
                possible_persons=disputed,
                links=governing,
                flags=flags,
            )
        if revoked:
            persons = sorted({link.person for link in revoked})
            return IdentityResolution(
                member=member,
                at=at,
                status="revoked",
                possible_persons=persons,
                links=governing,
                flags=flags,
            )
        return IdentityResolution(
            member=member,
            at=at,
            status="incomplete",
            links=governing,
            flags=flags or ["no_usable_identity_link"],
        )

    def person_identity_status(self, person: str, at: datetime) -> IdentityResolution:
        """Return the worst identity state touching a known person at ``at``."""
        member_ids = sorted({link.member for link in self.by_person.get(person, [])})
        resolutions = [self.resolve(member, at) for member in member_ids]
        bad = [r for r in resolutions if r.status != "confirmed"]
        if not bad:
            return IdentityResolution(member="", at=at, status="confirmed", person=person)
        precedence = {"conflict": 4, "disputed": 3, "incomplete": 2, "revoked": 1}
        worst = max(bad, key=lambda r: precedence.get(r.status, 0))
        worst.person = person
        return worst

    def possible_closure(
        self,
        member_ids: set[str] | None = None,
        person_ids: set[str] | None = None,
        at: datetime | None = None,
    ) -> tuple[set[str], set[str]]:
        """BFS across all non-revoked latest-version member/person edges."""
        members = set(member_ids or set())
        persons = set(person_ids or set())

        # For event-specific closure, latest governing rows at that time are
        # used.  Otherwise use the latest version of every relationship.
        if at is None:
            edges = list(self._global_governing.values())
        else:
            edge_keys = set()
            edges = []
            for member in self.by_member:
                for link in self.governing_links_at(member, at):
                    key = self._key(link)
                    if key not in edge_keys:
                        edge_keys.add(key)
                        edges.append(link)

        adjacency_member_to_persons: dict[str, set[str]] = defaultdict(set)
        adjacency_person_to_members: dict[str, set[str]] = defaultdict(set)
        for link in edges:
            if canonical_identity_state(link.state) == "revoked":
                continue
            adjacency_member_to_persons[link.member].add(link.person)
            adjacency_person_to_members[link.person].add(link.member)

        pending_members = list(members)
        pending_persons = list(persons)
        while pending_members:
            member = pending_members.pop()
            for person in adjacency_member_to_persons.get(member, set()):
                if person not in persons:
                    persons.add(person)
                    pending_persons.append(person)
        while pending_persons:
            person = pending_persons.pop()
            for member in adjacency_person_to_members.get(person, set()):
                if member not in members:
                    members.add(member)
                    pending_members.append(member)
            # Need to process newly added members after expanding persons; the
            # outer member queue may have been drained already, so directly
            # recurse via a work loop.
            while pending_members:
                member = pending_members.pop()
                for p2 in adjacency_member_to_persons.get(member, set()):
                    if p2 not in persons:
                        persons.add(p2)
                        pending_persons.append(p2)
        return members, persons

    def all_member_ids(self) -> set[str]:
        return set(self.by_member)
