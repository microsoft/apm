"""Per-target execution helpers for the marketplace publisher.

Contains the internal functions extracted from ``process_target`` that
handle per-consumer-target processing: guard checks, dependency updates,
atomic ``apm.yml`` writes, git commit/push, and the top-level
``_process_single_target`` driver.

These are private to the ``marketplace`` package and are imported by
``process_target`` so they remain accessible via the
``_process_target._process_single_target`` path used by
:class:`~apm_cli.marketplace.publisher.MarketplacePublisher`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..utils.path_security import PathTraversalError, ensure_path_within
from ._git_utils import redact_token as _redact_token
from .git_stderr import translate_git_stderr
from .publisher import ConsumerTarget, PublishOutcome, PublishPlan, TargetResult
from .resolver import parse_marketplace_ref
from .semver import parse_semver

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CommitContext:
    """Bundle git commit/push parameters for _commit_and_push."""

    run_git: object
    target: ConsumerTarget
    plan: PublishPlan
    clone_dir: Path
    tmpdir: str


def _check_update_guards(
    matches: list,
    new_ref: str,
    target: ConsumerTarget,
    plan: PublishPlan,
) -> TargetResult | None:
    """Return a short-circuit TargetResult if any guard fires, else None.

    Extracted from :func:`_process_single_target` to reduce its McCabe
    complexity, branch count, and statement count within the configured
    Ruff thresholds.
    """
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
                        f"Entry '{entry_str}' uses implicit latest; pass allow_ref_change to pin"
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
    return None


def _apply_dep_updates(apm_deps: list, matches: list, new_ref: str) -> tuple[str | None, int]:
    """Apply in-place ref updates to ``apm_deps``; return (first_old_ref, updated_count).

    Extracted from :func:`_process_single_target` to reduce its McCabe
    complexity, branch count, and statement count within the configured
    Ruff thresholds.
    """
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
    return first_old_ref, updated_count


def _write_apm_yml_atomic(apm_yml_path: Path, data: dict) -> None:
    """Write *data* to *apm_yml_path* using an atomic rename.

    Extracted from :func:`_process_single_target` to reduce its McCabe
    complexity, branch count, and statement count within the configured
    Ruff thresholds.
    """
    new_text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)  # yaml-io-exempt
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


def _commit_and_push(
    ctx: _CommitContext,
    dry_run: bool,
) -> TargetResult | None:
    """Git add + commit + optional push; return TargetResult on failure, None on success.

    Extracted from :func:`_process_single_target` to reduce its McCabe
    complexity, branch count, and statement count within the configured
    Ruff thresholds.
    """
    try:
        ctx.run_git(
            ["git", "add", ctx.target.path_in_repo],
            cwd=str(ctx.clone_dir),
        )
        msg_file = Path(ctx.tmpdir) / "commit-msg.txt"
        msg_file.write_text(ctx.plan.commit_message, encoding="utf-8")
        ctx.run_git(
            ["git", "commit", "-F", str(msg_file)],
            cwd=str(ctx.clone_dir),
        )
    except subprocess.CalledProcessError as exc:
        return TargetResult(
            target=ctx.target,
            outcome=PublishOutcome.FAILED,
            message=("Commit failed: " + _redact_token(str(exc))),
        )

    if not dry_run:
        try:
            ctx.run_git(
                [
                    "git",
                    "push",
                    "-u",
                    "origin",
                    ctx.plan.branch_name,
                ],
                cwd=str(ctx.clone_dir),
            )
        except subprocess.CalledProcessError as exc:
            stderr = _redact_token(exc.stderr or "")
            return TargetResult(
                target=ctx.target,
                outcome=PublishOutcome.FAILED,
                message=f"Push failed: {stderr}",
            )
    return None


def _load_parsed_data(target: ConsumerTarget, apm_yml_path: Path) -> TargetResult | dict:
    """Read and parse the apm.yml file; return the dict or a TargetResult on failure."""
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
            message=f"Failed to parse {target.path_in_repo}: {exc}",
        )
    if not isinstance(data, dict):
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message="Invalid apm.yml: expected a mapping",
        )
    return data


def _setup_clone_and_parse(
    self,
    target: ConsumerTarget,
    plan: PublishPlan,
    clone_dir: Path,
    tmpdir: str,
) -> TargetResult | tuple[Path, dict]:
    """Clone repo, create publish branch, load apm.yml; return (path, data) or TargetResult."""
    url = f"https://github.com/{target.repo}.git"
    try:
        self._run_git(
            ["git", "clone", "--depth=1", "--branch", target.branch, url, str(clone_dir)],
            cwd=tmpdir,
        )
    except subprocess.CalledProcessError as exc:
        stderr = _redact_token(exc.stderr or "")
        translated = translate_git_stderr(
            stderr, exit_code=exc.returncode, operation="clone", remote=target.repo
        )
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message=f"Clone failed: {translated.summary}",
        )

    try:
        self._run_git(["git", "checkout", "-B", plan.branch_name], cwd=str(clone_dir))
    except subprocess.CalledProcessError as exc:
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message="Branch creation failed: " + _redact_token(str(exc)),
        )

    apm_yml_path = clone_dir / target.path_in_repo
    try:
        ensure_path_within(apm_yml_path, clone_dir)
    except PathTraversalError:
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message="Path traversal rejected: " + target.path_in_repo,
        )

    load_result = _load_parsed_data(target, apm_yml_path)
    if isinstance(load_result, TargetResult):
        return load_result
    return apm_yml_path, load_result


def _find_matching_deps(
    target: ConsumerTarget,
    plan: PublishPlan,
    data: dict,
) -> TargetResult | tuple[list, str, list]:
    """Find marketplace dep entries; return (matches, new_ref, apm_deps) or TargetResult."""
    deps = data.get("dependencies")
    apm_deps = deps.get("apm") if isinstance(deps, dict) else None
    if not isinstance(apm_deps, list):
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message=f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml",
        )

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

    if not matches:
        warn_suffix = ""
        if warnings:
            warn_suffix = " (warnings: " + "; ".join(warnings) + ")"
        return TargetResult(
            target=target,
            outcome=PublishOutcome.FAILED,
            message=(
                f"Marketplace '{plan.marketplace_name}' not referenced in apm.yml{warn_suffix}"
            ),
        )
    return matches, new_ref, apm_deps


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

        setup_result = _setup_clone_and_parse(self, target, plan, clone_dir, tmpdir)
        if isinstance(setup_result, TargetResult):
            return setup_result
        apm_yml_path, data = setup_result

        find_result = _find_matching_deps(target, plan, data)
        if isinstance(find_result, TargetResult):
            return find_result
        matches, new_ref, apm_deps = find_result

        guard_result = _check_update_guards(matches, new_ref, target, plan)
        if guard_result is not None:
            return guard_result

        needs_update = any(old_ref != new_ref for _, _, old_ref, _ in matches)
        if not needs_update:
            return TargetResult(
                target=target,
                outcome=PublishOutcome.NO_CHANGE,
                message=f"Already at {new_ref}",
                old_version=new_ref,
                new_version=new_ref,
            )

        first_old_ref, updated_count = _apply_dep_updates(apm_deps, matches, new_ref)
        _write_apm_yml_atomic(apm_yml_path, data)
        commit_ctx = _CommitContext(
            run_git=self._run_git,
            target=target,
            plan=plan,
            clone_dir=clone_dir,
            tmpdir=tmpdir,
        )
        push_result = _commit_and_push(commit_ctx, dry_run)
        if push_result is not None:
            return push_result

        if updated_count == 1:
            msg = f"Updated {plan.marketplace_name} from {first_old_ref or 'unset'} to {new_ref}"
        else:
            msg = f"Updated {updated_count} entries for {plan.marketplace_name} to {new_ref}"
        return TargetResult(
            target=target,
            outcome=PublishOutcome.UPDATED,
            message=msg,
            old_version=first_old_ref,
            new_version=new_ref,
        )
