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
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .semver import SemVer

import yaml

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ._git_utils import redact_token as _redact_token
from ._publish_state import (
    ConsumerTarget as ConsumerTarget,
)
from ._publish_state import (
    PublishOutcome as PublishOutcome,
)
from ._publish_state import (
    PublishPlan as PublishPlan,
)
from ._publish_state import (
    PublishState as PublishState,
)
from ._publish_state import (
    TargetResult as TargetResult,
)
from .errors import MarketplaceError
from .git_stderr import translate_git_stderr
from .migration import load_marketplace_config
from .ref_resolver import RefResolver
from .resolver import parse_marketplace_ref
from .semver import parse_semver
from .tag_pattern import render_tag

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
# Publisher service
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 60


class MarketplacePublisher:
    """Update consumer repositories with new marketplace versions.

    Parameters
    ----------
    marketplace_root:
        Path to the marketplace repository root (must contain an
        ``apm.yml`` with a ``marketplace`` block, or the legacy
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
        """Lazy-load marketplace config (apm.yml or legacy marketplace.yml)."""
        if self._yml is None:
            self._yml = load_marketplace_config(self._root)
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

        Reads the local marketplace config (``apm.yml`` or legacy
        ``marketplace.yml``) to discover the marketplace name and version,
        validates all targets, and computes a deterministic branch name
        and commit message.

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
            If the marketplace config (``apm.yml`` or legacy
            ``marketplace.yml``) cannot be loaded or is invalid.
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

    # -- per-target helpers -------------------------------------------------

    def _load_consumer_manifest(
        self,
        clone_dir: Path,
        target: ConsumerTarget,
        plan: PublishPlan,
    ) -> tuple[dict | None, Path, TargetResult | None]:
        """Load and validate consumer apm.yml.

        Returns ``(data, apm_yml_path, None)`` on success or
        ``(None, apm_yml_path, TargetResult)`` on first error.
        """
        apm_yml_path = clone_dir / target.path_in_repo
        try:
            ensure_path_within(apm_yml_path, clone_dir)
        except PathTraversalError:
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message="Path traversal rejected: " + target.path_in_repo,
                ),
            )

        if not apm_yml_path.exists():
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"File not found: {target.path_in_repo}",
                ),
            )

        try:
            raw_text = apm_yml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_text)
        except (yaml.YAMLError, OSError) as exc:
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"Failed to parse {target.path_in_repo}: {exc}",
                ),
            )

        if not isinstance(data, dict):
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message="Invalid apm.yml: expected a mapping",
                ),
            )

        deps = data.get("dependencies")
        if not isinstance(deps, dict):
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml",
                ),
            )

        apm_deps = deps.get("apm")
        if not isinstance(apm_deps, list):
            return (
                None,
                apm_yml_path,
                TargetResult(
                    target=target,
                    outcome=PublishOutcome.FAILED,
                    message=f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml",
                ),
            )

        return data, apm_yml_path, None

    def _check_ref_guards(
        self,
        matches: list[tuple[int, str, str | None, str]],
        target: ConsumerTarget,
        plan: PublishPlan,
        new_sv: SemVer | None,
    ) -> TargetResult | None:
        """Check ref-change and downgrade guards. Returns error result or None."""
        new_ref = plan.new_ref
        for _idx, _pname, old_ref, entry_str in matches:
            if old_ref == new_ref:
                continue

            # Ref-change guard
            if old_ref is None:
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
                    if not plan.allow_ref_change:
                        return TargetResult(
                            target=target,
                            outcome=PublishOutcome.SKIPPED_REF_CHANGE,
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
                            outcome=PublishOutcome.SKIPPED_DOWNGRADE,
                            message=(
                                f"Downgrade from {old_ref} to "
                                f"{new_ref}; pass allow_downgrade "
                                "to override"
                            ),
                            old_version=old_ref,
                            new_version=new_ref,
                        )
        return None

    # -- per-target processing ----------------------------------------------

    def _clone_and_checkout(
        self,
        target: ConsumerTarget,
        plan: PublishPlan,
        tmpdir: str,
        clone_dir: Path,
    ) -> TargetResult | None:
        """Shallow-clone target repo and create the publish branch.

        Returns ``None`` on success, or a :class:`TargetResult` with
        ``FAILED`` outcome on any subprocess error.
        """
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
        return None

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

            # 1+2. Shallow clone + create publish branch
            clone_err = self._clone_and_checkout(target, plan, tmpdir, clone_dir)
            if clone_err is not None:
                return clone_err

            # 3. Load consumer apm.yml
            data, apm_yml_path, manifest_err = self._load_consumer_manifest(clone_dir, target, plan)
            if manifest_err is not None:
                return manifest_err

            # 4. Find matching marketplace entries in dependencies.apm
            apm_deps = data["dependencies"]["apm"]

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
            guard_err = self._check_ref_guards(matches, target, plan, new_sv)
            if guard_err is not None:
                return guard_err

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
