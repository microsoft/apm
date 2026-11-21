"""The queue runner: ``tick`` once, or ``run --interval 60``.

Shadow by default: trains are built and proven on queue refs, and nothing is written
to main. With ``--land`` the runner also admin-merges proven trains (landing.py);
every train-building write still goes through ShadowApi.

python -m scripts.merge_queue.runner tick  [--state-dir DIR] [--depth N] [--land]
python -m scripts.merge_queue.runner run   [--state-dir DIR] [--interval 60] [--land]
python -m scripts.merge_queue.runner metrics [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.merge_queue import landing, metrics, proof, refs, state, tickets, train
from scripts.merge_queue.api import REPO, Api, GhApi, Response, get_all

DEFAULT_STATE_DIR = Path.home() / ".apm-queue"
CODEQL_PREFIX = "Analyze ("
EXIT_LOCKED = 3


class ShadowViolation(RuntimeError):
    """A shadow runner tried to write to main or merge a PR."""


class ShadowApi:
    """Wraps an Api and refuses every write except creating, merging into and deleting
    queue refs: ``git/refs`` may only create ``queue/*`` or ``queue-build/*``, ``merges``
    may only target ``queue-build/*``, and deletes only remove those branches."""

    _REFS = f"repos/{REPO}/git/refs"
    _MERGES = f"repos/{REPO}/merges"

    def __init__(self, inner: Api) -> None:
        self.inner = inner

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.inner.get(path, params)

    def post(self, path: str, body: Mapping[str, Any]) -> Response:
        if path == self._REFS:
            target = str(body.get("ref", ""))
            if not target.startswith(("refs/heads/queue/", "refs/heads/queue-build/")):
                raise ShadowViolation(f"shadow runner refused to create {target!r}")
        elif path == self._MERGES:
            target = str(body.get("base", ""))
            if not target.startswith("queue-build/"):
                raise ShadowViolation(f"shadow runner refused a merge into {target!r}")
        else:
            raise ShadowViolation(f"shadow runner refused POST {path}")
        return self.inner.post(path, body)

    def patch(self, path: str, body: Mapping[str, Any]) -> Response:
        raise ShadowViolation(f"shadow runner refused PATCH {path}")

    def delete(self, path: str) -> Response:
        prefix = f"{self._REFS}/heads/"
        name = path.removeprefix(prefix)
        if not path.startswith(prefix) or not name.startswith(("queue/", "queue-build/")):
            raise ShadowViolation(f"shadow runner refused DELETE {path}")
        return self.inner.delete(path)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Log:
    def __init__(self, path: Path | None, clock: Callable[[], str] = _now) -> None:
        self.path, self.clock, self.events = path, clock, []

    def emit(self, event: str, **fields: Any) -> None:
        record = {"at": self.clock(), "event": event, **fields}
        self.events.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


def pr_facts(api: Api, pr: int) -> tuple[tickets.PrFacts, list[dict]]:
    pull = api.get(f"repos/{REPO}/pulls/{pr}")
    head = pull["head"]["sha"]
    runs = api.get(f"repos/{REPO}/commits/{head}/check-runs", {"per_page": 100})
    checks = {run["name"]: run for run in runs.get("check_runs", [])}
    gate = checks.get("gate", {})
    codeql = [run for name, run in checks.items() if name.startswith(CODEQL_PREFIX)]
    facts = tickets.PrFacts(
        head=head,
        labels=tuple(label["name"] for label in pull.get("labels", [])),
        base=pull["base"]["ref"],
        same_repo=(pull["head"].get("repo") or {}).get("full_name") == REPO,
        gate_green=gate.get("conclusion") == "success",
        codeql_done=bool(codeql) and all(run.get("status") == "completed" for run in codeql),
    )
    return facts, get_all(api, f"repos/{REPO}/issues/{pr}/comments")


def ready_tickets(api: Api, log: Log, skip: set[int]) -> list[tickets.Ticket]:
    issues = get_all(api, f"repos/{REPO}/issues", {"labels": tickets.READY_LABEL, "state": "open"})
    ready = []
    for issue in issues:
        pr = int(issue["number"])
        if "pull_request" not in issue or pr in skip:
            continue
        facts, comments = pr_facts(api, pr)
        ticket = tickets.latest_ticket(pr, comments)
        reason = "no ticket marker" if ticket is None else tickets.admission(ticket, facts)
        if reason is not None:
            log.emit("not-ready", pr=pr, reason=reason)
            continue
        files = get_all(api, f"repos/{REPO}/pulls/{pr}/files")
        lines = sum(int(f.get("changes", 0)) for f in files)
        ready.append(
            tickets.Ticket(
                pr,
                ticket.head,
                ticket.prio,
                ticket.ticketed_at,
                ticket.fixes,
                ticket.depends,
                tuple(f["filename"] for f in files),
                lines,
            )
        )
    return ready


def poll(api: Api, current: state.State, log: Log) -> None:
    """Record verdicts for in-flight trains; log the landings a real runner would make."""
    for item in current.in_flight():
        result, run_id = proof.verdict(api, item.head_ref, item.head_sha)
        if result not in (proof.GREEN, proof.RED):
            continue
        item.move(state.GREEN if result == proof.GREEN else state.RED)
        item.run_id, item.verdict_at = run_id, log.clock()
        log.emit("verdict", train=item.train_id, verdict=result, run=run_id)
        current.window = train.next_window(
            current.window, "green" if result == proof.GREEN else "red-unattributed"
        )
        if result == proof.GREEN:
            for member in item.members:
                log.emit(
                    "would-land",
                    train=item.train_id,
                    pr=member.pr,
                    k=member.k,
                    commit=member.sha,
                    tree=member.tree,
                )
        api.delete(f"repos/{REPO}/git/refs/heads/{refs.build_ref(item.train_id)}")


def satisfied_dependencies(
    api: Api, ready: list[tickets.Ticket], flying: list[state.Train]
) -> set[int]:
    """Dependencies already in the build base: merged PRs, or members of trains in flight."""
    aboard = {member.pr for train_ in flying for member in train_.members}
    wanted = {dep for ticket in ready for dep in ticket.depends} - aboard
    merged = {dep for dep in wanted if api.get(f"repos/{REPO}/pulls/{dep}").get("merged")}
    return aboard | merged


def _delete_refs(api: Api, names: list[str]) -> None:
    for name in names:
        api.delete(f"repos/{REPO}/git/refs/heads/{name}")


def _publish(api: Api, names: list[str], shas: list[str]) -> list[str] | None:
    """Create the prefix refs; on any failure delete what was created and return None."""
    created: list[str] = []
    for name, sha in zip(names, shas, strict=True):
        response = api.post(f"repos/{REPO}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha})
        if response.status not in (200, 201):
            _delete_refs(api, created)
            return None
        created.append(name)
    return created


def launch(api: Api, current: state.State, log: Log, main_sha: str, depth: int) -> None:
    """Build and publish one more train while fewer than ``depth`` are in flight."""
    flying = current.in_flight()
    if len(flying) >= depth:
        return
    ready = ready_tickets(api, log, current.queued_prs())
    landed = satisfied_dependencies(api, ready, flying)
    members = train.compose(ready, current.window, landed=landed)
    if not members:
        return
    # A stacked train starts at the previous train's head, but its Queue-Base
    # stays the merge base with main (merge-train.yml checks the trailer).
    tip = flying[-1].head_sha if flying else main_sha
    queue_base = flying[-1].base if flying else main_sha
    live = api.get(f"repos/{REPO}/git/matching-refs/heads/queue") or []
    train_id = refs.next_train_id(
        [str(ref["ref"]).removeprefix("refs/heads/") for ref in live], current.max_id
    )
    current.max_id = train_id
    built = train.build(api, train_id, tip, members, queue_base)
    for pr, why in built.deferred.items():
        log.emit("deferred", train=train_id, pr=pr, reason=why)
    for pr in built.in_base:
        log.emit("already-in-base", train=train_id, pr=pr)
    names = refs.prefix_refs(train_id, len(built.members))
    published = (
        None if built.error or not built.members else _publish(api, names, built.prefix_shas)
    )
    if published is None:
        _delete_refs(api, [refs.build_ref(train_id)])
        reason = built.error or (
            "no member merged" if not built.members else "ref publication failed"
        )
        log.emit("launch-failed", train=train_id, reason=reason)
        return
    current.trains[train_id] = state.Train(
        train_id,
        queue_base,
        [
            state.Member(t.pr, t.head, k, sha, tree)
            for k, (t, sha, tree) in enumerate(
                zip(built.members, built.prefix_shas, built.prefix_trees, strict=True), 1
            )
        ],
        names,
        published_at=log.clock(),
    )
    log.emit(
        "published", train=train_id, base=queue_base, tip=tip, prs=[t.pr for t in built.members]
    )


def sweep(api: Api, current: state.State, now: datetime) -> list[str]:
    """Delete queue refs older than 12 h that no tracked train owns (the janitor)."""
    known = {train_id for train_id, item in current.trains.items() if item.status == state.TESTING}
    listed = []
    for prefix in ("queue/t", "queue-build/t"):
        for ref in api.get(f"repos/{REPO}/git/matching-refs/heads/{prefix}") or []:
            name = str(ref["ref"]).removeprefix("refs/heads/")
            commit = api.get(f"repos/{REPO}/commits/{ref['object']['sha']}")
            stamp = commit["commit"]["committer"]["date"]
            listed.append(
                (name, datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC))
            )
    stale = refs.janitor(listed, known, now)
    _delete_refs(api, stale)
    return stale


def tick(
    api: Api, current: state.State, log: Log, depth: int = 2, now: datetime | None = None
) -> state.State:
    shadow = ShadowApi(api)
    poll(shadow, current, log)
    for name in sweep(shadow, current, now or datetime.now(UTC)):
        log.emit("janitor", ref=name)
    main_sha = shadow.get(f"repos/{REPO}/commits/main")["sha"]
    launch(shadow, current, log, main_sha, depth)
    return current


def _try_lock(handle: Any) -> bool:
    """Non-blocking exclusive lock on an open file: fcntl on POSIX, msvcrt on Windows."""
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def single_instance(lock_path: Path) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        yield _try_lock(handle)


def _one_tick(api: Api, state_dir: Path, depth: int, merge: landing.Merger | None = None) -> None:
    """One pass. ``merge`` set means --land: proven trains are landed after the shadow pass."""
    state_path = state_dir / "state.json"
    live = state.rebuild(api)
    current = state.reconcile(state.load(state_path), live) if state_path.is_file() else live
    log = Log(state_dir / "events.jsonl")
    tick(api, current, log, depth)
    if merge is not None:
        landing_path = state_dir / "landing.json"
        lander = landing.load(landing_path)
        landing.tick(api, current, lander, log, merge)
        landing.save(lander, landing_path)
    state.save(current, state_path)
    (state_dir / "heartbeat").write_text(_now() + "\n", encoding="utf-8")


def main(argv: Sequence[str], api: Api | None = None, merge: landing.Merger | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.merge_queue.runner")
    parser.add_argument("command", choices=("tick", "run", "metrics"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument(
        "--land", action="store_true", help="admin-merge proven trains (default: shadow only)"
    )
    args = parser.parse_args(argv)
    if args.command == "metrics":
        print(json.dumps(metrics.summarize(metrics.read_events(args.state_dir / "events.jsonl"))))
        return 0
    with single_instance(args.state_dir / "runner.lock") as owned:
        if not owned:
            print("another queue runner holds the lock; exiting", file=sys.stderr)
            return EXIT_LOCKED
        client = api or GhApi()
        while True:
            _one_tick(
                client,
                args.state_dir,
                args.depth,
                (merge or landing.gh_merge) if args.land else None,
            )
            if args.command == "tick":
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
