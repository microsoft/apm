"""Speculative ref names, parsing, id allocation and the janitor."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

SCOPES = ("guard", "full")
_TRAIN_REF = re.compile(r"^queue/t(?P<id>\d{4,})/p(?P<k>\d+)-(?P<scope>guard|full)$")
_BUILD_REF = re.compile(r"^queue-build/t(?P<id>\d{4,})$")
JANITOR_AGE = timedelta(hours=12)


def train_ref(train_id: int, k: int, scope: str) -> str:
    if scope not in SCOPES or k < 1:
        raise ValueError(f"bad train ref: t{train_id} p{k} {scope}")
    return f"queue/t{train_id:04d}/p{k}-{scope}"


def build_ref(train_id: int) -> str:
    return f"queue-build/t{train_id:04d}"


def base_probe_ref(sha: str) -> str:
    return f"queue/b{sha[:12]}/p0-full"


@dataclass(frozen=True)
class TrainRef:
    train_id: int
    k: int
    scope: str


def parse_train_ref(name: str) -> TrainRef | None:
    match = _TRAIN_REF.match(name)
    if match is None:
        return None
    return TrainRef(int(match["id"]), int(match["k"]), match["scope"])


def ref_train_id(name: str) -> int | None:
    parsed = parse_train_ref(name)
    if parsed is not None:
        return parsed.train_id
    match = _BUILD_REF.match(name)
    return int(match["id"]) if match else None


def next_train_id(existing_refs: Iterable[str], state_max: int) -> int:
    """Monotonic: one past the larger of the state's maximum and any live ref."""
    ids = [train_id for name in existing_refs if (train_id := ref_train_id(name)) is not None]
    return max([state_max, *ids]) + 1


def prefix_refs(train_id: int, members: int, pipeline: bool = False) -> list[str]:
    """BATCH: guard prefixes then one full head. PIPELINE: every prefix is full."""
    return [
        train_ref(train_id, k, "full" if pipeline or k == members else "guard")
        for k in range(1, members + 1)
    ]


def janitor(refs: Iterable[tuple[str, datetime]], known_ids: set[int], now: datetime) -> list[str]:
    """Queue refs older than 12 h whose train the state no longer tracks."""
    stale = []
    for name, created in refs:
        train_id = ref_train_id(name)
        if train_id is not None and train_id not in known_ids and now - created > JANITOR_AGE:
            stale.append(name)
    return sorted(stale)
