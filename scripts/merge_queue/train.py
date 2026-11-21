"""Train composition (pure) and server-side build through the merges API."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from scripts.merge_queue.api import REPO, Api
from scripts.merge_queue.refs import build_ref
from scripts.merge_queue.tickets import Ticket

BIG_PR_LINES = 1500
WINDOW_START, WINDOW_MAX, WINDOW_MIN = 4, 6, 2


def compose(ready: Sequence[Ticket], window: int, landed: set[int]) -> list[Ticket]:
    """Pick and order one train's members (design section 3).

    - lane P0..P3, then ticket time;
    - a member whose Depends-On PRs have not landed joins only behind all of them;
    - at most one big PR (over 1,500 changed lines), first within its lane;
    - a member touching .github/workflows trains alone: alone if it is first in
      line, otherwise it waits for a train of its own.
    """
    ordered = sorted(ready, key=lambda t: (t.lane, t.ticketed_at, t.pr))
    if ordered and ordered[0].touches_workflows:
        return [ordered[0]]
    chosen: list[Ticket] = []
    have_big = False
    for ticket in ordered:
        if len(chosen) >= window:
            break
        if ticket.touches_workflows:
            continue
        aboard = {member.pr for member in chosen}
        if any(dep not in landed and dep not in aboard for dep in ticket.depends):
            continue
        big = ticket.changed_lines > BIG_PR_LINES
        if big and have_big:
            continue
        have_big = have_big or big
        chosen.append(ticket)
    return _big_first_in_lane(chosen)


def _big_first_in_lane(members: list[Ticket]) -> list[Ticket]:
    out = list(members)
    for index, ticket in enumerate(members):
        if ticket.changed_lines > BIG_PR_LINES:
            start = next(i for i, other in enumerate(out) if other.lane == ticket.lane)
            blocked = any(dep in {m.pr for m in out[start:index]} for dep in ticket.depends)
            if not blocked:
                out.insert(start, out.pop(index))
            break
    return out


def next_window(window: int, outcome: str) -> int:
    """Zuul-like adaptive window: +1 per green, halve after an unattributed red."""
    if outcome == "green":
        return min(window + 1, WINDOW_MAX)
    if outcome == "red-unattributed":
        return max(window // 2, WINDOW_MIN)
    return window


def trailer(train_id: int, k: int, ticket: Ticket, base: str) -> str:
    return (
        f"Apm-Train: t{train_id:04d} k={k} pr=#{ticket.pr} head={ticket.head} base={base}\n"
        f"Queue-Base: {base}"
    )


@dataclass
class BuildResult:
    members: list[Ticket] = field(default_factory=list)
    prefix_shas: list[str] = field(default_factory=list)
    deferred: dict[int, str] = field(default_factory=dict)
    error: str | None = None


def build(api: Api, train_id: int, base: str, members: Sequence[Ticket]) -> BuildResult:
    """Merge each member onto queue-build/t<id> in order; a 409 defers that member."""
    result = BuildResult()
    branch = build_ref(train_id)
    created = api.post(f"repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base})
    if created.status not in (200, 201):
        result.error = f"could not create {branch}: {created.status} {created.body}"
        return result
    for ticket in members:
        k = len(result.members) + 1
        merged = api.post(
            f"repos/{REPO}/merges",
            {
                "base": branch,
                "head": ticket.head,
                "commit_message": f"queue: train t{train_id:04d} adds #{ticket.pr}\n\n"
                + trailer(train_id, k, ticket, base),
            },
        )
        if merged.status == 409:
            result.deferred[ticket.pr] = "conflicts with the base or an earlier member"
        elif merged.status in (200, 201):
            result.members.append(ticket)
            result.prefix_shas.append(str(merged.body["sha"]))
        elif merged.status == 204:
            result.deferred[ticket.pr] = "nothing to merge (already on the base)"
        else:
            result.error = f"merge of #{ticket.pr} failed: {merged.status} {merged.body}"
            return result
    return result
