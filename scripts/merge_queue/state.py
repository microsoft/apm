"""Runner state: a cache under ~/.apm-queue; ground truth is rebuilt from GitHub."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scripts.merge_queue.api import REPO, Api
from scripts.merge_queue.refs import parse_train_ref
from scripts.merge_queue.train import WINDOW_START

TESTING, GREEN, RED, ABORTED = "TESTING", "GREEN", "RED", "ABORTED"
TRANSITIONS = {
    TESTING: {GREEN, RED, ABORTED},
    GREEN: set(),
    RED: set(),
    ABORTED: set(),
}
_TRAILER = re.compile(
    r"^Apm-Train: t(?P<id>\d+) k=(?P<k>\d+) pr=#(?P<pr>\d+) head=(?P<head>[0-9a-f]{40})"
    r" base=(?P<base>[0-9a-f]{40})$",
    re.MULTILINE,
)


@dataclass
class Member:
    pr: int
    head: str
    k: int
    sha: str


@dataclass
class Train:
    train_id: int
    base: str
    members: list[Member]
    refs: list[str]
    status: str = TESTING
    published_at: str = ""
    verdict_at: str = ""
    run_id: int | None = None

    @property
    def head_ref(self) -> str:
        return self.refs[-1]

    @property
    def head_sha(self) -> str:
        return self.members[-1].sha

    def move(self, status: str) -> None:
        if status not in TRANSITIONS[self.status]:
            raise ValueError(f"t{self.train_id}: {self.status} -> {status} is not allowed")
        self.status = status


@dataclass
class State:
    trains: dict[int, Train] = field(default_factory=dict)
    max_id: int = 0
    window: int = WINDOW_START

    def in_flight(self) -> list[Train]:
        return sorted(
            (train for train in self.trains.values() if train.status == TESTING),
            key=lambda train: train.train_id,
        )

    def queued_prs(self) -> set[int]:
        return {m.pr for t in self.trains.values() if t.status == TESTING for m in t.members}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> State:
        raw = json.loads(text)
        trains = {
            int(key): Train(**{**value, "members": [Member(**m) for m in value["members"]]})
            for key, value in raw.get("trains", {}).items()
        }
        return cls(trains=trains, max_id=raw.get("max_id", 0), window=raw.get("window", 4))


def load(path: Path) -> State:
    return State.from_json(path.read_text(encoding="utf-8")) if path.is_file() else State()


def save(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.to_json(), encoding="utf-8")
    tmp.replace(path)


def rebuild(api: Api) -> State:
    """Recover in-flight trains from the live queue/t* refs and their commit trailers."""
    refs = api.get(f"repos/{REPO}/git/matching-refs/heads/queue/t") or []
    by_train: dict[int, list[tuple[int, str, str]]] = {}
    for ref in refs:
        name = str(ref["ref"]).removeprefix("refs/heads/")
        parsed = parse_train_ref(name)
        if parsed is not None:
            by_train.setdefault(parsed.train_id, []).append((parsed.k, name, ref["object"]["sha"]))
    state = State(max_id=max(by_train, default=0))
    for train_id, prefixes in by_train.items():
        prefixes.sort()
        members, base = [], ""
        for k, _name, sha in prefixes:
            message = api.get(f"repos/{REPO}/commits/{sha}")["commit"]["message"]
            match = next((m for m in _TRAILER.finditer(message) if int(m["k"]) == k), None)
            if match is None:
                break
            base = match["base"]
            members.append(Member(int(match["pr"]), match["head"], k, sha))
        if members and len(members) == len(prefixes):
            refs_sorted = [name for _k, name, _sha in prefixes]
            state.trains[train_id] = Train(train_id, base, members, refs_sorted)
    return state
