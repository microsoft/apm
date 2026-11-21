"""Train composition (pure) and server-side build through the merges API."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from scripts.merge_queue.api import REPO, Api, Response
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
    first = ordered[0] if ordered else None
    if first is not None and first.touches_workflows and set(first.depends) <= landed:
        return [first]
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


def trailer(train_id: int, k: int, ticket: Ticket, queue_base: str) -> str:
    """``Queue-Base`` must equal the merge base with main (merge-train.yml checks it)."""
    return (
        f"Apm-Train: t{train_id:04d} k={k} pr=#{ticket.pr} head={ticket.head} base={queue_base}\n"
        f"Queue-Base: {queue_base}"
    )


@dataclass
class BuildResult:
    members: list[Ticket] = field(default_factory=list)
    prefix_shas: list[str] = field(default_factory=list)
    prefix_trees: list[str] = field(default_factory=list)
    deferred: dict[int, str] = field(default_factory=dict)
    in_base: list[int] = field(default_factory=list)
    error: str | None = None


def _merge_one(
    api: Api, branch: str, train_id: int, ticket: Ticket, queue_base: str, k: int
) -> Response:
    return api.post(
        f"repos/{REPO}/merges",
        {
            "base": branch,
            "head": ticket.head,
            "commit_message": f"queue: train t{train_id:04d} adds #{ticket.pr}\n\n"
            + trailer(train_id, k, ticket, queue_base),
        },
    )


def build(
    api: Api, train_id: int, tip: str, members: Sequence[Ticket], queue_base: str
) -> BuildResult:
    """Merge each member onto queue-build/t<id> (created at ``tip``) in order.

    ``tip`` is where the train starts (main, or the head of a train still in
    flight); ``queue_base`` is main's SHA written into the trailers. A 409
    defers the member and, transitively, every later member depending on it.
    A 204 means the head is already in the base: satisfied, nothing to add.
    """
    result = BuildResult()
    branch = build_ref(train_id)
    created = api.post(f"repos/{REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": tip})
    if created.status not in (200, 201):
        result.error = f"could not create {branch}: {created.status} {created.body}"
        return result
    blocked: set[int] = set()
    for ticket in members:
        if blocked.intersection(ticket.depends):
            result.deferred[ticket.pr] = "depends on a deferred member"
            blocked.add(ticket.pr)
            continue
        merged = _merge_one(api, branch, train_id, ticket, queue_base, len(result.members) + 1)
        if merged.status == 409:
            result.deferred[ticket.pr] = "conflicts with the base or an earlier member"
            blocked.add(ticket.pr)
        elif merged.status == 204:
            result.in_base.append(ticket.pr)
        elif merged.status in (200, 201):
            result.members.append(ticket)
            result.prefix_shas.append(str(merged.body["sha"]))
            result.prefix_trees.append(str(merged.body["commit"]["tree"]["sha"]))
        else:
            result.error = f"merge of #{ticket.pr} failed: {merged.status} {merged.body}"
            return result
    return result
