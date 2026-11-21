"""The shadow runner: ``tick`` once, or ``run --interval 60``. Never writes to main.

python -m scripts.merge_queue.runner tick  [--state-dir DIR] [--depth N]
python -m scripts.merge_queue.runner run   [--state-dir DIR] [--interval 60]
python -m scripts.merge_queue.runner metrics [--state-dir DIR]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.merge_queue import metrics, proof, refs, state, tickets, train
from scripts.merge_queue.api import REPO, Api, GhApi, Response

DEFAULT_STATE_DIR = Path.home() / ".apm-queue"
CODEQL_PREFIX = "Analyze ("
EXIT_LOCKED = 3


class ShadowViolation(RuntimeError):
    """A shadow runner tried to write to main or merge a PR."""


class ShadowApi:
    """Wraps an Api and refuses every write outside queue refs and the merges API."""

    _ALLOWED_POSTS = (f"repos/{REPO}/git/refs", f"repos/{REPO}/merges")

    def __init__(self, inner: Api) -> None:
        self.inner = inner

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.inner.get(path, params)

    def post(self, path: str, body: Mapping[str, Any]) -> Response:
        if path not in self._ALLOWED_POSTS:
            raise ShadowViolation(f"shadow runner refused POST {path}")
        target = str(body.get("ref") or body.get("base") or "")
        if not target.removeprefix("refs/heads/").startswith(("queue/", "queue-build/")):
            raise ShadowViolation(f"shadow runner refused a write to {target!r}")
        return self.inner.post(path, body)

    def delete(self, path: str) -> Response:
        name = path.split("/git/refs/heads/", 1)[-1]
        if "/git/refs/heads/" not in path or not name.startswith(("queue/", "queue-build/")):
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
    comments = api.get(f"repos/{REPO}/issues/{pr}/comments", {"per_page": 100})
    return facts, comments


def ready_tickets(api: Api, log: Log, skip: set[int]) -> list[tickets.Ticket]:
    issues = api.get(
        f"repos/{REPO}/issues", {"labels": tickets.READY_LABEL, "state": "open", "per_page": 100}
    )
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
        files = api.get(f"repos/{REPO}/pulls/{pr}/files", {"per_page": 100})
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
                    "would-land", train=item.train_id, pr=member.pr, k=member.k, tree=member.sha
                )
        api.delete(f"repos/{REPO}/git/refs/heads/{refs.build_ref(item.train_id)}")


def launch(api: Api, current: state.State, log: Log, main_sha: str, depth: int) -> None:
    """Build and publish one more train while fewer than ``depth`` are in flight."""
    flying = current.in_flight()
    if len(flying) >= depth:
        return
    ready = ready_tickets(api, log, current.queued_prs())
    members = train.compose(ready, current.window, landed=set())
    if not members:
        return
    base = flying[-1].head_sha if flying else main_sha
    live = api.get(f"repos/{REPO}/git/matching-refs/heads/queue") or []
    train_id = refs.next_train_id(
        [str(ref["ref"]).removeprefix("refs/heads/") for ref in live], current.max_id
    )
    built = train.build(api, train_id, base, members)
    for pr, why in built.deferred.items():
        log.emit("deferred", train=train_id, pr=pr, reason=why)
    if built.error or not built.members:
        log.emit("build-failed", train=train_id, reason=built.error or "no member merged")
        return
    names = refs.prefix_refs(train_id, len(built.members))
    for name, sha in zip(names, built.prefix_shas, strict=True):
        api.post(f"repos/{REPO}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha})
    current.max_id = train_id
    current.trains[train_id] = state.Train(
        train_id,
        base,
        [
            state.Member(t.pr, t.head, k, sha)
            for k, (t, sha) in enumerate(zip(built.members, built.prefix_shas, strict=True), 1)
        ],
        names,
        published_at=log.clock(),
    )
    log.emit("published", train=train_id, base=base, prs=[t.pr for t in built.members])


def tick(api: Api, current: state.State, log: Log, depth: int = 2) -> state.State:
    shadow = ShadowApi(api)
    poll(shadow, current, log)
    main_sha = shadow.get(f"repos/{REPO}/commits/main")["sha"]
    launch(shadow, current, log, main_sha, depth)
    return current


@contextmanager
def single_instance(lock_path: Path) -> Iterator[bool]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _one_tick(api: Api, state_dir: Path, depth: int) -> None:
    state_path = state_dir / "state.json"
    current = state.load(state_path) if state_path.is_file() else state.rebuild(api)
    tick(api, current, Log(state_dir / "events.jsonl"), depth)
    state.save(current, state_path)
    (state_dir / "heartbeat").write_text(_now() + "\n", encoding="utf-8")


def main(argv: Sequence[str], api: Api | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.merge_queue.runner")
    parser.add_argument("command", choices=("tick", "run", "metrics"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--interval", type=int, default=60)
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
            _one_tick(client, args.state_dir, args.depth)
            if args.command == "tick":
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
