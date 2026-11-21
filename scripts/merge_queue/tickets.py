"""Landing tickets: the merge-worker's marker comment plus the ``queue:ready`` label."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

READY_LABEL = "queue:ready"
LANES = ("P0", "P1", "P2", "P3")
_MARKER = re.compile(
    r"<!--\s*apm-queue-ticket\s+head=(?P<head>[0-9a-f]{40})\s+prio=(?P<prio>P[0-3])"
    r"\s+fixes=\[(?P<fixes>[^\]]*)\]\s+depends=\[(?P<depends>[^\]]*)\]\s*-->"
)


def _numbers(text: str) -> tuple[int, ...]:
    return tuple(int(item.strip().lstrip("#")) for item in text.split(",") if item.strip())


@dataclass(frozen=True)
class Ticket:
    pr: int
    head: str
    prio: str
    ticketed_at: str
    fixes: tuple[int, ...] = ()
    depends: tuple[int, ...] = ()
    files: tuple[str, ...] = field(default=(), compare=False)
    changed_lines: int = field(default=0, compare=False)

    @property
    def lane(self) -> int:
        return LANES.index(self.prio)

    @property
    def touches_workflows(self) -> bool:
        return any(path.startswith(".github/workflows/") for path in self.files)


def parse_ticket(pr: int, comment_body: str, created_at: str) -> Ticket | None:
    """The ticket carried by one comment, or None when it has no valid marker."""
    match = _MARKER.search(comment_body)
    if match is None:
        return None
    return Ticket(
        pr=pr,
        head=match["head"],
        prio=match["prio"],
        ticketed_at=created_at,
        fixes=_numbers(match["fixes"]),
        depends=_numbers(match["depends"]),
    )


def latest_ticket(pr: int, comments: list[dict]) -> Ticket | None:
    """The newest valid ticket among a PR's comments (a re-ticket replaces the old one)."""
    found = [
        ticket
        for comment in comments
        if (ticket := parse_ticket(pr, str(comment.get("body", "")), str(comment["created_at"])))
    ]
    return max(found, key=lambda ticket: ticket.ticketed_at) if found else None


@dataclass(frozen=True)
class PrFacts:
    head: str
    labels: tuple[str, ...]
    base: str
    same_repo: bool
    gate_green: bool
    codeql_done: bool


def admission(ticket: Ticket, facts: PrFacts) -> str | None:
    """Why a ticket is not admissible, or None when it is READY."""
    if READY_LABEL not in facts.labels:
        return f"missing the {READY_LABEL} label"
    if facts.head != ticket.head:
        return f"head moved after the ticket ({facts.head[:12]} != {ticket.head[:12]})"
    if facts.base != "main":
        return f"base is {facts.base}, not main"
    if not facts.same_repo:
        return "fork PR: needs a maintainer approval of the exact head SHA"
    if not facts.gate_green:
        return "gate is not green at the ticketed head"
    if not facts.codeql_done:
        return "CodeQL has not completed at the ticketed head"
    return None
