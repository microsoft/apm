"""Marketplace publisher service -- update consumer repos with new versions.

Provides ``MarketplacePublisher`` for updating marketplace version
references in consumer repositories.  The publisher reads the local
``marketplace.yml``, computes a deterministic branch name and commit
message, then clones each consumer repo, updates its ``apm.yml``, and
pushes a feature branch.

This module is a library only -- no CLI wiring.  The CLI command
(``apm marketplace publish``) is wired in a later wave.

Design
------
* **Byte integrity**: the publisher NEVER modifies or regenerates
  ``marketplace.json`` content.  It only copies the file as-is from
  the marketplace source repo.
* **Token redaction**: stderr from git subprocesses is redacted via
  ``_git_utils.redact_token``.
* **Atomic writes**: state files and consumer ``apm.yml`` updates use
  write-tmp + ``os.fsync`` + ``os.replace``.
* **Error isolation**: failures in one target never abort other targets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional  # noqa: F401

import yaml

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ._git_utils import redact_token as _redact_token
from ._io import atomic_write
from .errors import MarketplaceError, MarketplaceYmlError  # noqa: F401
from .git_stderr import translate_git_stderr
from .ref_resolver import RefResolver
from .resolver import parse_marketplace_ref
from .semver import parse_semver
from .tag_pattern import render_tag
from .yml_schema import load_marketplace_yml

logger = logging.getLogger(__name__)

__all__ = [
    "ConsumerTarget",
    "MarketplacePublisher",
    "PublishOutcome",
    "PublishPlan",
    "PublishState",
    "TargetResult",
]

# ---------------------------------------------------------------------------
# Token redaction -- delegated to _git_utils; alias kept for call-site compat.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Branch name sanitisation
# ---------------------------------------------------------------------------

_BRANCH_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")

# Pattern for safe git remote URLs (HTTPS or SSH).
_SAFE_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

# Shell metacharacters that must never appear in branch names or repo slugs.
_SHELL_META_RE = re.compile(r"[;&|`$(){}!<>\"\']")


def _sanitise_branch_segment(text: str) -> str:
    """Replace characters that are unsafe for git branch names with hyphens."""
    return _BRANCH_UNSAFE_RE.sub("-", text)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
_BRANCH_SAFE_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


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


# ---------------------------------------------------------------------------
# Publisher service
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 60


class MarketplacePublisher:
    """Update consumer repositories with new marketplace versions.

    Parameters
    ----------
    marketplace_root:
        Path to the marketplace repository root (must contain
        ``marketplace.yml``).
    ref_resolver:
        Optional ``RefResolver`` instance (reserved for future use).
    clock:
        Callable returning the current ``datetime`` (injectable for
        tests).
    runner:
        Callable with the same signature as ``subprocess.run``
        (injectable for tests).
    """

    def __init__(
        self,
        marketplace_root: Path,
        *,
        ref_resolver: RefResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._root = marketplace_root.resolve()
        self._ref_resolver = ref_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._runner = runner or subprocess.run
        self._yml = None

    def _load_yml(self):
        """Lazy-load marketplace.yml."""
        if self._yml is None:
            yml_path = self._root / "marketplace.yml"
            self._yml = load_marketplace_yml(yml_path)
        return self._yml

    # -- plan ---------------------------------------------------------------

    def plan(
        self,
        targets: Sequence[ConsumerTarget],
        *,
        target_package: str | None = None,
        allow_downgrade: bool = False,
        allow_ref_change: bool = False,
    ) -> PublishPlan:
        """Compute a publish plan.

        Reads the local ``marketplace.yml`` to discover the marketplace
        name and version, validates all targets, and computes a
        deterministic branch name and commit message.

        Parameters
        ----------
        targets:
            Consumer repositories to update.
        target_package:
            If set, only update the reference for this specific package.
            If ``None``, bump the marketplace version across all targets.
        allow_downgrade:
            Allow version downgrades (new < old).
        allow_ref_change:
            Allow switching from an explicit ref to a version range.

        Returns
        -------
        PublishPlan
            Frozen plan ready for ``execute()``.

        Raises
        ------
        MarketplaceYmlError
            If ``marketplace.yml`` cannot be loaded or is invalid.
        PathTraversalError
            If any target's ``path_in_repo`` is a path traversal.
        """
        yml = self._load_yml()

        # Validate path_in_repo for each target
        for target in targets:
            validate_path_segments(
                target.path_in_repo,
                context=f"path_in_repo for {target.repo}",
            )

        # Validate repo and branch for each target
        for target in targets:
            # Repo must be a safe "owner/repo" slug with no shell metacharacters.
            if _SHELL_META_RE.search(target.repo):
                raise MarketplaceError(
                    f"Consumer target repo '{target.repo}' contains "
                    f"prohibited shell metacharacters."
                )
            if not _SAFE_REPO_RE.match(target.repo):
                raise MarketplaceError(
                    f"Consumer target repo '{target.repo}' must match "
                    f"'owner/repo' (alphanumeric, dots, hyphens, underscores)."
                )
            # Branch must not contain traversal sequences or shell metacharacters.
            validate_path_segments(
                target.branch,
                context=f"consumer target branch for {target.repo}",
            )
            if _SHELL_META_RE.search(target.branch):
                raise MarketplaceError(
                    f"Consumer target branch '{target.branch}' for "
                    f"'{target.repo}' contains prohibited shell metacharacters."
                )

        # Compute short hash
        sorted_repos = sorted(t.repo for t in targets)
        hash_input = "|".join(sorted_repos) + "|" + yml.version
        if target_package:
            hash_input += "|" + target_package
        short_hash = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()[:8]  # noqa: S324

        # Compute branch name
        name_segment = _sanitise_branch_segment(yml.name)
        version_segment = _sanitise_branch_segment(yml.version)
        branch_name = f"apm/marketplace-update-{name_segment}-{version_segment}-{short_hash}"

        # Compute commit message
        commit_message = (
            f"chore(apm): bump {yml.name} to {yml.version}\n"
            f"\n"
            f"Updated by apm marketplace publish.\n"
            f"\n"
            f"APM-Publish-Id: {short_hash}"
        )

        # Compute tag for the new version
        tag_pattern = yml.build.tag_pattern
        new_ref = render_tag(tag_pattern, name=yml.name, version=yml.version)

        return PublishPlan(
            marketplace_name=yml.name,
            marketplace_version=yml.version,
            targets=tuple(targets),
            commit_message=commit_message,
            branch_name=branch_name,
            new_ref=new_ref,
            tag_pattern_used=tag_pattern,
            short_hash=short_hash,
            allow_downgrade=allow_downgrade,
            allow_ref_change=allow_ref_change,
            target_package=target_package,
        )

    # -- execute ------------------------------------------------------------

    def execute(
        self,
        plan: PublishPlan,
        *,
        dry_run: bool = False,
        parallel: int = 4,
    ) -> list[TargetResult]:
        """Execute a publish plan.

        Iterates targets in parallel, updating each consumer's
        ``apm.yml`` with the new marketplace version.

        Parameters
        ----------
        plan:
            Plan computed by ``plan()``.
        dry_run:
            If ``True``, do not push changes to remote.
        parallel:
            Maximum number of concurrent target updates.

        Returns
        -------
        list[TargetResult]
            Results in the same order as ``plan.targets``.
        """
        state = PublishState.load(self._root)
        state.begin_run(plan)

        results: dict[int, TargetResult] = {}

        def _process(idx: int, target: ConsumerTarget) -> TargetResult:
            try:
                return self._process_single_target(target, plan, dry_run=dry_run)
            except Exception as exc:
                logger.debug("Target processing failed for %s", target.repo, exc_info=True)
                return TargetResult(
                    outcome=PublishOutcome.FAILED,
                    message=_redact_token(str(exc)),
                )

        workers = max(1, min(parallel, len(plan.targets)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_idx = {
                pool.submit(_process, idx, target): idx for idx, target in enumerate(plan.targets)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.debug("Future result failed for target %d", idx, exc_info=True)
                    result = TargetResult(
                        target=plan.targets[idx],
                        outcome=PublishOutcome.FAILED,
                        message=_redact_token(str(exc)),
                    )
                results[idx] = result
                state.record_result(result)

        state.finalise(self._clock())

        # Return in plan.targets order
        return [results[i] for i in range(len(plan.targets))]

    # -- per-target processing ----------------------------------------------

    def _process_single_target(
        self,
        target: ConsumerTarget,
        plan: PublishPlan,
        *,
        dry_run: bool = False,
    ) -> TargetResult:
        """Clone, update, commit, and optionally push a single target."""
        with tempfile.TemporaryDirectory(prefix="apm-publish-") as tmpdir:
            clone_dir = Path(tmpdir) / "repo"

            # 1. Shallow clone
            url = f"https://github.com/{target.repo}.git"
            try:
                self._run_git(
                    [
                        "git",
                        "clone",
                        "--depth=1",
                        "--branch",
                        target.branch,
                        url,
                        str(clone_dir),
                    ],
                    cwd=tmpdir,
                )
            except subprocess.CalledProcessError as exc:
                stderr = _redact_token(exc.stderr or "")
                translated = translate_git_stderr(
                    stderr,
                    exit_code=exc.returncode,
                    operation="clone",
                    remote=target.repo,
                )
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"Clone failed: {translated.summary}",
                )

            # 2. Create publish branch
            try:
                self._run_git(
                    ["git", "checkout", "-B", plan.branch_name],
                    cwd=str(clone_dir),
                )
            except subprocess.CalledProcessError as exc:
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=("Branch creation failed: " + _redact_token(str(exc))),
                )

            # 3. Load consumer apm.yml
            apm_yml_path = clone_dir / target.path_in_repo
            try:
                ensure_path_within(apm_yml_path, clone_dir)
            except PathTraversalError:
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=("Path traversal rejected: " + target.path_in_repo),
                )

            if not apm_yml_path.exists():
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"File not found: {target.path_in_repo}",
                )

            try:
                raw_text = apm_yml_path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw_text)
            except (yaml.YAMLError, OSError) as exc:
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=(f"Failed to parse {target.path_in_repo}: {exc}"),
                )

            if not isinstance(data, dict):
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message="Invalid apm.yml: expected a mapping",
                )

            # 4. Find matching marketplace entries in dependencies.apm
            deps = data.get("dependencies")
            if not isinstance(deps, dict):
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=(f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml"),
                )

            apm_deps = deps.get("apm")
            if not isinstance(apm_deps, list):
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=(f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml"),
                )

            # Parse each entry with parse_marketplace_ref
            new_ref = plan.new_ref
            mkt_lower = plan.marketplace_name.lower()
            matches: list[tuple[int, str, str | None, str]] = []
            warnings: list[str] = []

            for idx, entry_str in enumerate(apm_deps):
                if not isinstance(entry_str, str):
                    continue
                try:
                    parsed = parse_marketplace_ref(entry_str)
                except ValueError as exc:
                    warnings.append(str(exc))
                    continue
                if parsed is None:
                    continue  # Direct repo ref -- not a marketplace entry
                _plugin_name, entry_mkt, old_ref = parsed
                if entry_mkt.lower() == mkt_lower:
                    matches.append((idx, _plugin_name, old_ref, entry_str))

            # 5. Zero matches -> FAILED
            if not matches:
                warn_suffix = ""
                if warnings:
                    warn_suffix = " (warnings: " + "; ".join(warnings) + ")"
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=(
                        f"Marketplace '{plan.marketplace_name}' not "
                        f"referenced in apm.yml{warn_suffix}"
                    ),
                )

            # 6. Guards -- check every entry that would change
            new_sv = parse_semver(new_ref.lstrip("vV"))

            for _idx, _pname, old_ref, entry_str in matches:
                if old_ref == new_ref:
                    continue  # Already at target -- no guard needed

                # Ref-change guard
                if old_ref is None:
                    # Implicit latest -> explicit pin
                    if not plan.allow_ref_change:
                        return TargetResult(
                            target=target,
                            outcome=PublishOutcome.SKIPPED_REF_CHANGE,
                            message=(
                                f"Entry '{entry_str}' uses implicit "
                                "latest; pass allow_ref_change to pin"
                            ),
                            old_version=None,
                            new_version=new_ref,
                        )
                else:
                    old_sv = parse_semver(old_ref.lstrip("vV"))
                    if old_sv is None and new_sv is not None:
                        # Non-semver ref -> semver tag
                        if not plan.allow_ref_change:
                            return TargetResult(
                                target=target,
                                outcome=(PublishOutcome.SKIPPED_REF_CHANGE),
                                message=(
                                    f"Entry '{entry_str}' uses "
                                    f"non-semver ref '{old_ref}'; "
                                    "pass allow_ref_change to switch"
                                ),
                                old_version=old_ref,
                                new_version=new_ref,
                            )

                    # Downgrade guard
                    if old_sv and new_sv and new_sv < old_sv:
                        if not plan.allow_downgrade:
                            return TargetResult(
                                target=target,
                                outcome=(PublishOutcome.SKIPPED_DOWNGRADE),
                                message=(
                                    f"Downgrade from {old_ref} to "
                                    f"{new_ref}; pass allow_downgrade "
                                    "to override"
                                ),
                                old_version=old_ref,
                                new_version=new_ref,
                            )

            # 7. No-change check
            needs_update = any(old_ref != new_ref for _, _, old_ref, _ in matches)
            if not needs_update:
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.NO_CHANGE,
                    message=f"Already at {new_ref}",
                    old_version=new_ref,
                    new_version=new_ref,
                )

            # 8. Apply updates to matching entries
            first_old_ref: str | None = None
            updated_count = 0
            for idx, _pname, old_ref, entry_str in matches:
                if old_ref == new_ref:
                    continue
                if first_old_ref is None:
                    first_old_ref = old_ref
                if "#" in entry_str:
                    base = entry_str.split("#", 1)[0]
                    apm_deps[idx] = f"{base}#{new_ref}"
                else:
                    apm_deps[idx] = f"{entry_str}#{new_ref}"
                updated_count += 1

            # 9. Write apm.yml atomically
            new_text = yaml.safe_dump(
                data, default_flow_style=False, sort_keys=False
            )  # yaml-io-exempt
            tmp_yml = apm_yml_path.with_suffix(".yml.tmp")
            try:
                with open(tmp_yml, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp_yml), str(apm_yml_path))
            except BaseException:
                try:  # noqa: SIM105
                    tmp_yml.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

            # 10. Git add + commit
            try:
                self._run_git(
                    ["git", "add", target.path_in_repo],
                    cwd=str(clone_dir),
                )
                msg_file = Path(tmpdir) / "commit-msg.txt"
                msg_file.write_text(plan.commit_message, encoding="utf-8")
                self._run_git(
                    ["git", "commit", "-F", str(msg_file)],
                    cwd=str(clone_dir),
                )
            except subprocess.CalledProcessError as exc:
                return TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=("Commit failed: " + _redact_token(str(exc))),
                )

            # 11. Git push (unless dry_run)
            if not dry_run:
                try:
                    self._run_git(
                        [
                            "git",
                            "push",
                            "-u",
                            "origin",
                            plan.branch_name,
                        ],
                        cwd=str(clone_dir),
                    )
                except subprocess.CalledProcessError as exc:
                    stderr = _redact_token(exc.stderr or "")
                    return TargetResult(
                        target=target,
                        outcome=PublishOutcome.FAILED,
                        message=f"Push failed: {stderr}",
                    )

            old_label = first_old_ref or "unset"
            if updated_count == 1:
                msg = f"Updated {plan.marketplace_name} from {old_label} to {new_ref}"
            else:
                msg = f"Updated {updated_count} entries for {plan.marketplace_name} to {new_ref}"
            return TargetResult(
                target=target,
                outcome=PublishOutcome.UPDATED,
                message=msg,
                old_version=first_old_ref,
                new_version=new_ref,
            )

    # -- git runner ---------------------------------------------------------

    def _run_git(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = _GIT_TIMEOUT,
    ) -> subprocess.CompletedProcess:
        """Run a git command via the injectable runner."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
        return self._runner(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=env,
        )

    # -- safe force push ----------------------------------------------------

    def safe_force_push(
        self,
        remote: str,
        branch_name: str,
        expected_trailer: str,
    ) -> bool:
        """Force-push only if the remote branch head has the expected trailer.

        Checks that the remote branch's HEAD commit message contains
        ``APM-Publish-Id: <expected_trailer>``.  If it does, performs
        a ``git push --force-with-lease``; otherwise refuses silently.

        Returns ``True`` on push success, ``False`` if refused or on
        any error.  Never raises for the trailer-mismatch case.
        """
        try:
            result = self._run_git(
                [
                    "git",
                    "log",
                    "--format=%B",
                    "-1",
                    f"{remote}/{branch_name}",
                ],
                cwd=str(self._root),
            )
            commit_msg = result.stdout.strip()

            trailer_line = f"APM-Publish-Id: {expected_trailer}"
            if trailer_line not in commit_msg:
                return False

            self._run_git(
                [
                    "git",
                    "push",
                    "--force-with-lease",
                    remote,
                    branch_name,
                ],
                cwd=str(self._root),
            )
            return True
        except subprocess.CalledProcessError:
            return False
