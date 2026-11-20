"""Oracle receipts: the only credit the lifecycle obligation gate accepts (#4093).

A receipt records that one oracle law applied to one CLI step and passed.
Only ``tests.lifecycle.oracles`` may write one: ``emit`` refuses any other
caller, so a label, a skip, a cell event or a requested step can never earn
credit.

Receipts live in memory for the current process and, when the lifecycle
plugin configured a directory (``APM_LIFECYCLE_RECEIPTS_DIR``), in one JSONL
file per process. The plugin merges a session's per-process files at the end
of the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

DIRECTORY_ENV = "APM_LIFECYCLE_RECEIPTS_DIR"
SESSION_ENV = "APM_LIFECYCLE_RECEIPTS_SESSION"
OWNER_MODULE = "tests.lifecycle.oracles"
OUTCOMES = ("success", "refusal")

_EARNED: list[Receipt] = []


class ReceiptForgeryError(RuntimeError):
    """A receipt was written by something other than an oracle."""


@dataclass(frozen=True)
class Receipt:
    node: str
    params: Mapping[str, object]
    pre_digest: str
    argv: tuple[str, ...]
    scope: str
    post_digest: str
    oracle_id: str
    outcome: str
    lane: str = field(default="")

    def to_json(self) -> str:
        data = asdict(self)
        data["params"] = dict(self.params)
        data["argv"] = list(self.argv)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> Receipt:
        data = json.loads(line)
        data["argv"] = tuple(data["argv"])
        return cls(**data)


def emit(receipt: Receipt) -> None:
    """Record *receipt*; only the oracle module may call this."""
    caller = sys._getframe(1).f_globals.get("__name__")
    if caller != OWNER_MODULE:
        raise ReceiptForgeryError(f"receipts are written only by {OWNER_MODULE}, not {caller}")
    if receipt.outcome not in OUTCOMES:
        raise ValueError(f"unknown receipt outcome {receipt.outcome!r}")
    _EARNED.append(receipt)
    directory = os.environ.get(DIRECTORY_ENV)
    if directory:
        path = Path(directory) / f"{_session()}-{_worker()}.part.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="ascii") as handle:
            handle.write(receipt.to_json() + "\n")


def earned(node: str | None = None) -> tuple[Receipt, ...]:
    """Receipts this process earned, optionally only those of one test node."""
    if node is None:
        return tuple(_EARNED)
    wanted = normalize_node(node)
    return tuple(receipt for receipt in _EARNED if receipt.node == wanted)


def current_node() -> str:
    """The running pytest node id, without the phase and any xdist group suffix."""
    return normalize_node(os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" ", 1)[0])


def current_lane() -> str:
    """The test family lane that earned the receipt, from the node path."""
    node = current_node()
    if node.startswith("tests/lifecycle/"):
        parts = node.split("::", 1)[0].split("/")
        return parts[2] if len(parts) > 3 else "core"
    return node.split("/", 2)[1] if node.count("/") >= 2 else ""


def normalize_node(node: str) -> str:
    """Drop the ``@group`` suffix xdist's loadgroup scheduler appends."""
    return re.sub(r"@[\w.-]+$", "", node)


def load(directory: Path) -> list[Receipt]:
    """Every receipt of the completed (merged) sessions under *directory*.

    Per-process ``.part.jsonl`` files belong to a session still running, so a
    reader in that session never sees a witness half-way through.
    """
    found: list[Receipt] = []
    for path in sorted(directory.glob("session-*.jsonl")) if directory.is_dir() else ():
        found.extend(_read(path))
    return found


def load_session(path: Path) -> list[Receipt]:
    """Every receipt of one merged session file."""
    return list(_read(path))


def merge_session(directory: Path, session: str) -> Path | None:
    """Merge one session's per-process files into ``session-<id>.jsonl``."""
    parts = sorted(directory.glob(f"{session}-*.part.jsonl"))
    if not parts:
        return None
    merged = directory / f"session-{session}.jsonl"
    lines = [line for part in parts for line in part.read_text(encoding="ascii").splitlines()]
    merged.write_text("".join(f"{line}\n" for line in lines if line), encoding="ascii")
    for part in parts:
        part.unlink()
    return merged


def drop_completed_sessions(directory: Path, *, keep: Path) -> None:
    """Remove merged sessions other than *keep*, so a reader judges only the newest run.

    Receipts from earlier partial runs, older code or an older catalog would
    otherwise make a witness that did not run this time look under-covered.
    """
    for path in directory.glob("session-*.jsonl") if directory.is_dir() else ():
        if path != keep:
            path.unlink(missing_ok=True)


def _read(path: Path) -> Iterable[Receipt]:
    for line in path.read_text(encoding="ascii").splitlines():
        if line.strip():
            yield Receipt.from_json(line)


def _session() -> str:
    return os.environ.get(SESSION_ENV, "local")


def _worker() -> str:
    return f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}-{os.getpid()}"
