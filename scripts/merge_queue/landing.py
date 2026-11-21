"""The landing loop: admin-merge proven trains onto main, one member at a time.

Landing is off unless the runner is started with ``--land``. Each member lands with
``gh pr merge --merge --admin --match-head-commit <proven head>`` only when main's tree
is the tree the member was proven on, and the merge commit must have the expected
parents and the proven tree T_k. Any drift HALTs landing (trains keep testing) until a
human clears ``landing.json``.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from scripts.merge_queue import closure, state, tickets
from scripts.merge_queue.api import REPO, Api, get_all

LAPSE_PROBES = 2
PER_MERGE_SECONDS = 20
MAX_MERGE_ATTEMPTS = 3
RETRY_SECONDS = 5
RULE_VIOLATIONS = (
    "repository rule violations",
    "merge queue",
    "required status check",
    "not authorized",
)
LANDED, PAUSED = "landed", "paused"


class Halt(Exception):
    """Main is not where the proof says it must be; stop landing."""


class Merger(Protocol):
    def __call__(self, pr: int, head: str) -> tuple[bool, str]: ...


def gh_merge(pr: int, head: str) -> tuple[bool, str]:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "pr",
            "merge",
            str(pr),
            "--repo",
            REPO,
            "--merge",
            "--admin",
            "--match-head-commit",
            head,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def is_rule_violation(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in RULE_VIOLATIONS)


@dataclass
class Elevation:
    """Admin elevation, with the 2-probe lapse rule: one failed probe only skips a
    tick; two in a row declare a lapse (one needs_input), and the first good probe
    after a lapse resumes landing."""

    misses: int = 0
    lapsed: bool = False
    lapses: int = 0

    def observe(self, admin: bool) -> str | None:
        if admin:
            self.misses = 0
            if self.lapsed:
                self.lapsed = False
                return "resume"
            return None
        self.misses += 1
        if self.misses >= LAPSE_PROBES and not self.lapsed:
            self.lapsed, self.lapses = True, self.lapses + 1
            return "lapse"
        return None


@dataclass
class LandingState:
    elevation: Elevation = field(default_factory=Elevation)
    halted: str = ""
    main_seen: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)
    notified: list[int] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> LandingState:
        raw = json.loads(text)
        return cls(
            Elevation(**raw.get("elevation", {})),
            raw.get("halted", ""),
            raw.get("main_seen", ""),
            raw.get("pending", []),
            raw.get("notified", []),
        )


def load(path: Path) -> LandingState:
    return (
        LandingState.from_json(path.read_text(encoding="utf-8"))
        if path.is_file()
        else LandingState()
    )


def save(current: LandingState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(current.to_json(), encoding="utf-8")
    tmp.replace(path)


def probe(api: Api) -> bool:
    return bool((api.get(f"repos/{REPO}").get("permissions") or {}).get("admin"))


def lease_seconds(trains: list[state.Train]) -> int:
    """How long the elevation must last to land the whole backlog."""
    return PER_MERGE_SECONDS * sum(1 for t in trains for m in t.members if not m.merged)


def ticket_fixes(api: Api, pr: int) -> tuple[int, ...]:
    ticket = tickets.latest_ticket(pr, get_all(api, f"repos/{REPO}/issues/{pr}/comments"))
    return ticket.fixes if ticket is not None else ()


def _pre_tree(api: Api, item: state.Train, member: state.Member) -> str:
    """The tree main must have for ``member`` to land as proven: the prefix before it."""
    if member.k > 1:
        return item.members[member.k - 2].tree
    first = api.get(f"repos/{REPO}/commits/{member.sha}")
    tip = api.get(f"repos/{REPO}/commits/{first['parents'][0]['sha']}")
    return str(tip["commit"]["tree"]["sha"])


def _check_pull(api: Api, member: state.Member) -> dict[str, Any]:
    pull = api.get(f"repos/{REPO}/pulls/{member.pr}")
    if pull.get("merged"):
        raise Halt(f"#{member.pr} was merged outside the runner")
    if pull["head"]["sha"] != member.head:
        raise Halt(f"#{member.pr} head moved after its proof ({pull['head']['sha'][:12]})")
    if pull["base"]["ref"] != "main":
        raise Halt(f"#{member.pr} is based on {pull['base']['ref']}, not main")
    return pull


def _merge(merge: Merger, member: state.Member, sleep: Callable[[float], None]) -> tuple[bool, str]:
    """Merge with a bounded retry on rule violations; (ok, last message)."""
    message = ""
    for attempt in range(1, MAX_MERGE_ATTEMPTS + 1):
        ok, message = merge(member.pr, member.head)
        if ok or not is_rule_violation(message):
            return ok, message
        if attempt < MAX_MERGE_ATTEMPTS:
            sleep(RETRY_SECONDS)
    return False, message


def _verify(api: Api, member: state.Member, before: str) -> str:
    """The merge commit must be main, with parents (old main, head) and tree T_k."""
    merged = api.get(f"repos/{REPO}/pulls/{member.pr}").get("merge_commit_sha") or ""
    commit = api.get(f"repos/{REPO}/commits/{merged}") if merged else {}
    parents = [p["sha"] for p in commit.get("parents", [])]
    tree = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if parents != [before, member.head] or tree != member.tree:
        raise Halt(
            f"#{member.pr} merged as {merged[:12]} with parents {[p[:12] for p in parents]}"
            f" and tree {str(tree)[:12]}, not the proven T_k {member.tree[:12]}"
        )
    main = api.get(f"repos/{REPO}/commits/main")["sha"]
    if main != merged:
        raise Halt(f"main is {main[:12]} right after landing #{member.pr} as {merged[:12]}")
    return merged


def land_member(
    api: Api,
    merge: Merger,
    item: state.Train,
    member: state.Member,
    current: LandingState,
    log: Any,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Land one member. Returns LANDED or PAUSED, raises Halt on drift."""
    main = api.get(f"repos/{REPO}/commits/main")
    if current.main_seen and main["sha"] != current.main_seen:
        raise Halt(f"main moved outside the runner: {current.main_seen[:12]} -> {main['sha'][:12]}")
    if main["commit"]["tree"]["sha"] != _pre_tree(api, item, member):
        raise Halt(f"main's tree is not t{item.train_id} k={member.k - 1}'s proven tree")
    pull = _check_pull(api, member)
    found = closure.audit(api, member.pr, ticket_fixes(api, member.pr))
    log.emit(
        "closure-audit",
        pr=member.pr,
        fixes=list(found.fixes),
        closing=list(found.closing),
        missing=found.missing,
        extra=found.extra,
    )
    ok, message = _merge(merge, member, sleep)
    if not ok:
        if is_rule_violation(message):
            log.emit("merge-refused", train=item.train_id, pr=member.pr, reason=message)
            return PAUSED
        raise Halt(f"#{member.pr} merge failed: {message}")
    member.merged = _verify(api, member, main["sha"])
    current.main_seen = member.merged
    current.pending.append(
        {**asdict(found), "head_ref": pull["head"]["ref"], "merged": member.merged}
    )
    log.emit(
        "landed",
        train=item.train_id,
        pr=member.pr,
        k=member.k,
        commit=member.merged,
        tree=member.tree,
    )
    return LANDED


def land_train(
    api: Api,
    merge: Merger,
    item: state.Train,
    current: LandingState,
    log: Any,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    for member in item.members:
        if member.merged:
            continue
        if land_member(api, merge, item, member, current, log, sleep) == PAUSED:
            return PAUSED
    item.move(state.LANDED)
    return LANDED


def settle_pending(api: Api, current: LandingState, log: Any) -> None:
    """Close or reopen issues for earlier landings, once GitHub's own closing has run."""
    for entry in current.pending:
        found = closure.Audit(entry["pr"], tuple(entry["fixes"]), tuple(entry["closing"]))
        actions = closure.settle(api, found)
        moved = closure.retarget_stacked(api, entry["head_ref"])
        log.emit("closure-settled", pr=found.pr, actions=actions, retargeted=moved)
    current.pending = []


def notify_ejections(runner_state: state.State, current: LandingState, log: Any) -> None:
    """One ejection notice per member of each red train, for autopilot-comment to post."""
    for item in sorted(runner_state.trains.values(), key=lambda t: t.train_id):
        if item.status != state.RED or item.train_id in current.notified:
            continue
        for member in item.members:
            log.emit(
                "ejection-notice",
                train=item.train_id,
                pr=member.pr,
                run=item.run_id,
                body=(
                    f"Merge train t{item.train_id:04d} went red (run {item.run_id}); "
                    f"#{member.pr} was not landed. Re-ticket it once the cause is fixed."
                ),
            )
        current.notified.append(item.train_id)


def tick(
    api: Api,
    runner_state: state.State,
    current: LandingState,
    log: Any,
    merge: Merger = gh_merge,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """One landing pass: settle earlier closures, then land the proven backlog in order."""
    settle_pending(api, current, log)
    if current.halted:
        log.emit("halted", reason=current.halted)
        return
    change = current.elevation.observe(probe(api))
    if change == "lapse":
        log.emit(
            "needs-input",
            lapse=current.elevation.lapses,
            reason="admin elevation lapsed: landing paused, trains keep testing",
        )
    elif change == "resume":
        log.emit("resume", lapse=current.elevation.lapses)
    notify_ejections(runner_state, current, log)
    if current.elevation.misses:
        return
    main = api.get(f"repos/{REPO}/commits/main")["sha"]
    if current.main_seen and main != current.main_seen:
        current.halted = f"main moved outside the runner: {current.main_seen[:12]} -> {main[:12]}"
        log.emit("halt", reason=current.halted)
        return
    current.main_seen = main
    backlog = runner_state.proven()
    if not backlog:
        return
    log.emit("lease", seconds=lease_seconds(backlog), trains=[t.train_id for t in backlog])
    for item in backlog:
        try:
            if land_train(api, merge, item, current, log, sleep) == PAUSED:
                return
        except Halt as stop:
            current.halted = str(stop)
            log.emit("halt", train=item.train_id, reason=current.halted)
            return
