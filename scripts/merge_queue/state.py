"""Runner state: a cache under ~/.apm-queue; ground truth is rebuilt from GitHub."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scripts.merge_queue.api import REPO, Api
from scripts.merge_queue.refs import parse_train_ref, prefix_refs, ref_train_id
from scripts.merge_queue.train import WINDOW_START

TESTING, GREEN, RED, ABORTED, LANDED = "TESTING", "GREEN", "RED", "ABORTED", "LANDED"
TRANSITIONS = {
    TESTING: {GREEN, RED, ABORTED},
    GREEN: {LANDED, ABORTED},
    RED: set(),
    ABORTED: set(),
    LANDED: set(),
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
    tree: str = ""
    merged: str = ""


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

    def proven(self) -> list[Train]:
        """The landing backlog: GREEN trains in launch order, up to the first one still
        TESTING (a later train was built on it and cannot land before it)."""
        backlog = []
        for train in sorted(self.trains.values(), key=lambda train: train.train_id):
            if train.status == TESTING:
                break
            if train.status == GREEN:
                backlog.append(train)
        return backlog

    def chain(self) -> list[Train]:
        """Trains a new train must stack on when landing: testing, or proven but unlanded."""
        return sorted(
            (
                t
                for t in self.trains.values()
                if t.status == TESTING
                or (t.status == GREEN and not all(m.merged for m in t.members))
            ),
            key=lambda train: train.train_id,
        )

    def queued_prs(self) -> set[int]:
        """PRs aboard a train that is testing or proven but not yet fully landed."""
        return {
            m.pr
            for t in self.trains.values()
            if t.status in (TESTING, GREEN)
            for m in t.members
            if not m.merged
        }

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


def _members_from_ancestry(api: Api, head_sha: str) -> tuple[list[Member], str]:
    """Walk first parents from a train head, reading one Apm-Train trailer per member.

    merge-train.yml deletes each guard ref after its run, so the surviving refs
    are not the member inventory; the head commit's ancestry is.
    """
    members: list[Member] = []
    base, sha = "", head_sha
    while True:
        commit = api.get(f"repos/{REPO}/commits/{sha}")
        match = _TRAILER.search(commit["commit"]["message"])
        if match is None:
            return [], ""
        base = match["base"]
        members.append(
            Member(
                int(match["pr"]),
                match["head"],
                int(match["k"]),
                sha,
                commit["commit"]["tree"]["sha"],
            )
        )
        if int(match["k"]) == 1:
            return list(reversed(members)), base
        sha = commit["parents"][0]["sha"]


def _live_heads(api: Api) -> dict[int, tuple[str, str]]:
    """train id -> (head ref name, head sha): the -full ref, else the retained build branch."""
    heads: dict[int, tuple[str, str]] = {}
    for ref in api.get(f"repos/{REPO}/git/matching-refs/heads/queue-build/t") or []:
        name = str(ref["ref"]).removeprefix("refs/heads/")
        train_id = ref_train_id(name)
        if train_id is not None:
            heads[train_id] = ("", ref["object"]["sha"])
    for ref in api.get(f"repos/{REPO}/git/matching-refs/heads/queue/t") or []:
        name = str(ref["ref"]).removeprefix("refs/heads/")
        parsed = parse_train_ref(name)
        if parsed is not None and parsed.scope == "full":
            heads[parsed.train_id] = (name, ref["object"]["sha"])
    return heads


def rebuild(api: Api) -> State:
    """Recover in-flight trains from live refs and their commits' trailers."""
    state = State()
    for train_id, (full_ref, head_sha) in sorted(_live_heads(api).items()):
        state.max_id = max(state.max_id, train_id)
        members, base = _members_from_ancestry(api, head_sha)
        if not members:
            continue
        refs = prefix_refs(train_id, len(members))
        if full_ref and full_ref != refs[-1]:
            continue
        state.trains[train_id] = Train(train_id, base, members, refs)
    return state


def reconcile(cached: State, live: State) -> State:
    """Add live trains the cache lost (a crash between publish and save); keep cached history."""
    for train_id, train in live.trains.items():
        cached.trains.setdefault(train_id, train)
    cached.max_id = max(cached.max_id, live.max_id)
    return cached
