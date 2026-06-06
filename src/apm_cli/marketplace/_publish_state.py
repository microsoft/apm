"""Publish data model and transactional state file.

Extracted from publisher.py to keep module complexity bounded.
All public symbols are re-exported from publisher.py so existing
import paths (tests, patches) keep working unchanged.

No module-level import of publisher.py (cycle-safe).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..utils.path_security import ensure_path_within
from ._io import atomic_write

# ---------------------------------------------------------------------------
# Validation regexes (used by ConsumerTarget)
# ---------------------------------------------------------------------------

_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
_BRANCH_SAFE_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumerTarget:
    """A consumer repository whose ``apm.yml`` should be updated."""

    repo: str  # e.g. "acme-org/service-a"
    branch: str = "main"  # base branch on the consumer to PR into
    path_in_repo: str = "apm.yml"  # location of the consumer's apm.yml

    def __post_init__(self) -> None:
        if not _REPO_RE.match(self.repo):
            raise ValueError(
                f"ConsumerTarget.repo must be in 'owner/name' format "
                f"using only alphanumerics, dots, hyphens, and underscores. "
                f"Got: {self.repo!r}"
            )
        if not _BRANCH_SAFE_RE.match(self.branch) or ".." in self.branch:
            raise ValueError(
                f"ConsumerTarget.branch contains disallowed characters. "
                f"Only alphanumerics, dots, hyphens, underscores, and "
                f"forward slashes are permitted (no '..' sequences). "
                f"Got: {self.branch!r}"
            )
        from ..utils.path_security import validate_path_segments

        validate_path_segments(self.path_in_repo, context="consumer-targets path_in_repo")


@dataclass(frozen=True)
class PublishPlan:
    """Computed plan for a publish run -- frozen and deterministic."""

    marketplace_name: str  # name from the local marketplace.yml
    marketplace_version: str  # version from the local marketplace.yml
    targets: tuple[ConsumerTarget, ...]
    commit_message: str  # pre-computed, contains the APM trailer
    branch_name: str  # pre-computed, deterministic
    new_ref: str  # rendered tag, e.g. "v2.0.0"
    tag_pattern_used: str  # tag pattern, e.g. "v{version}"
    short_hash: str = ""  # deterministic hash suffix for the branch name
    allow_downgrade: bool = False
    allow_ref_change: bool = False
    target_package: str | None = None


class PublishOutcome(str, Enum):
    """Outcome of processing a single consumer target."""

    UPDATED = "updated"
    NO_CHANGE = "no-change"
    SKIPPED_DOWNGRADE = "skipped-downgrade"
    SKIPPED_REF_CHANGE = "skipped-ref-change"
    FAILED = "failed"


@dataclass(frozen=True)
class TargetResult:
    """Result of processing a single consumer target."""

    target: ConsumerTarget
    outcome: PublishOutcome
    message: str  # human-readable detail
    old_version: str | None = None
    new_version: str | None = None


# ---------------------------------------------------------------------------
# Transactional state file
# ---------------------------------------------------------------------------

_STATE_FILENAME = "publish-state.json"
_STATE_DIR = ".apm"
_MAX_HISTORY = 10
_SCHEMA_VERSION = 1


class PublishState:
    """Transactional state file for publish runs.

    State is persisted at ``.apm/publish-state.json`` relative to the
    marketplace repo root.  All writes are atomic (write-tmp + fsync +
    ``os.replace``).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._state_dir = self._root / _STATE_DIR
        self._state_path = self._state_dir / _STATE_FILENAME
        self._data: dict[str, Any] = {
            "schemaVersion": _SCHEMA_VERSION,
            "lastRun": None,
            "history": [],
        }

    @classmethod
    def load(cls, root: Path) -> PublishState:
        """Load state from disk or return a fresh instance.

        A missing file or corrupt JSON both result in a fresh state --
        no exception is raised.
        """
        instance = cls(root)
        if instance._state_path.exists():
            try:
                text = instance._state_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, dict):
                    instance._data = data
            except (json.JSONDecodeError, OSError):
                pass  # start fresh on corrupt state
        return instance

    def _atomic_write(self) -> None:
        """Write state atomically via temp file + fsync + os.replace.

        Path validation and directory creation happen here; the actual
        write is delegated to the shared ``atomic_write()`` helper from
        ``_io.py``.
        """
        ensure_path_within(self._state_dir, self._root)
        self._state_dir.mkdir(parents=True, exist_ok=True)

        content = json.dumps(self._data, indent=2) + "\n"
        atomic_write(self._state_path, content)

    def begin_run(self, plan: PublishPlan) -> None:
        """Start a new publish run -- writes ``startedAt``."""
        self._data["lastRun"] = {
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "finishedAt": None,
            "marketplaceName": plan.marketplace_name,
            "marketplaceVersion": plan.marketplace_version,
            "branchName": plan.branch_name,
            "results": [],
        }
        self._atomic_write()

    def record_result(self, result: TargetResult) -> None:
        """Append a target result to the current run."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["results"].append(
            {
                "repo": result.target.repo,
                "outcome": result.outcome.value,
                "message": result.message,
                "oldVersion": result.old_version,
                "newVersion": result.new_version,
            }
        )
        self._atomic_write()

    def finalise(self, finished_at: datetime) -> None:
        """Finalise the current run and rotate history."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["finishedAt"] = finished_at.isoformat()

        # Rotate history -- keep at most _MAX_HISTORY entries
        history = self._data.get("history", [])
        history.insert(0, dict(self._data["lastRun"]))
        self._data["history"] = history[:_MAX_HISTORY]
        self._atomic_write()

    def abort(self, reason: str) -> None:
        """Mark the current run as aborted."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["finishedAt"] = f"ABORTED: {reason}"
        self._atomic_write()

    @property
    def data(self) -> dict[str, Any]:
        """Return the raw state data (read-only snapshot for inspection)."""
        return dict(self._data)
