"""Local deploy helpers extracted from local_bundle.py."""

from __future__ import annotations

import builtins
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.utils.diagnostics import DiagnosticCollector

set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass(frozen=True, slots=True)
class _DeployFlags:
    """Deploy-time flags threaded through local-bundle helpers."""

    force: bool
    dry_run: bool
    diagnostics: Any
    logger: Any


def _stage_instruction_dest(
    rel: str,
    slug: Any,
    project_root: Path,
    logger: InstallLogger | None,
) -> tuple[Path, Path] | None:
    """Resolve stage dest for a bundled instruction on a compile-only target.

    Called when the current target lacks the ``"instructions"`` primitive
    (e.g. opencode, codex, gemini) so the file must be staged under
    ``apm_modules/<slug>/.apm/instructions/`` for ``apm compile`` to pick
    up later.

    Performs strict slug validation before constructing any filesystem path.

    Returns ``(dest, stage_root)`` on success, or ``None`` when the slug
    is invalid (the caller should increment *skipped* and ``continue``).
    """
    from apm_cli.utils.path_security import (
        PathTraversalError,
        ensure_path_within,
        validate_path_segments,
    )

    _slug_str = str(slug)
    # CR1.5 (#1217 review): ASCII-only validation -- str.isalnum() accepts
    # non-Latin Unicode chars which would slip past [A-Za-z0-9._-].
    _ALLOWED = builtins.set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    _slug_ok = (
        bool(_slug_str)
        and all(c in _ALLOWED for c in _slug_str)
        and not _slug_str.startswith(".")
        and not _slug_str.endswith(".")
        and ".." not in _slug_str
    )
    if not _slug_ok:
        if logger is not None:
            logger.warning(
                f"Skipped instruction staging for unsafe slug {_slug_str!r}: "
                "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
            )
        return None
    try:
        validate_path_segments(_slug_str, context="bundle slug")
    except PathTraversalError as exc:
        if logger is not None:
            logger.warning(f"Skipped instruction staging for unsafe slug {_slug_str!r}: {exc}")
        return None
    stage_root = project_root / "apm_modules" / slug / ".apm" / "instructions"
    try:
        ensure_path_within(stage_root, project_root / "apm_modules")
    except PathTraversalError as exc:
        if logger is not None:
            logger.warning(f"Skipped unsafe stage root for {slug!r}: {exc}")
        return None
    # PR #1217 review: preserve nested subdirs under ``instructions/`` so
    # two files with the same basename do not collide at the staged location.
    _rel_under_instructions = rel.split("/", 1)[1] if "/" in rel else Path(rel).name
    dest = stage_root / _rel_under_instructions
    return dest, stage_root


def _compute_bundle_record(
    dest: Path,
    project_root: Path,
    scope: Any,
) -> str:
    """Return the lockfile key string for a deployed bundle file.

    User-scope installs use absolute paths; project-scope installs use
    ``project_root``-relative POSIX paths (with absolute fallback when
    *dest* is outside *project_root*).
    """
    from apm_cli.core.scope import InstallScope

    try:
        if scope == InstallScope.USER:
            return dest.as_posix()
        else:
            return (
                dest.relative_to(project_root).as_posix()
                if dest.is_relative_to(project_root)
                else dest.as_posix()
            )
    except ValueError:
        return dest.as_posix()


def _deploy_file(
    src: Path,
    dest: Path,
    record: str,
    expected_hash: str,
    flags: _DeployFlags,
) -> tuple[str | None, str | None, bool]:
    """Deploy a single bundle file to *dest*.

    Handles dry-run (no writes), collision detection (skip when content
    differs and *force* is False), and the actual copy + hash.

    Returns ``(record, file_hash, was_skipped)``:

    * On dry-run: ``(record, "sha256:<hex>", False)``
    * On skip:    ``(None, None, True)``
    * On deploy:  ``(record, "sha256:<hex>", False)``
    """
    from apm_cli.utils.content_hash import compute_file_hash

    force = flags.force
    dry_run = flags.dry_run
    diagnostics = flags.diagnostics
    logger = flags.logger
    if dry_run:
        if logger:
            logger.verbose_detail(f"[dry-run] would deploy {record}")
        # Normalize to "sha256:<hex>" so the dry-run lockfile preview matches
        # the format written by ``compute_file_hash`` on the real deploy path.
        return record, f"sha256:{expected_hash}", False

    # Collision handling: skip if file exists with different content (unless
    # --force).  Idempotent (same-content) writes are allowed through.
    if dest.exists() and not force:
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None
        if existing_hash and existing_hash != expected_hash:
            msg = (
                f"Skipped {record}: file exists with different "
                "content. Re-run with --force to overwrite."
            )
            if diagnostics is not None:
                diagnostics.warn(msg)
            elif logger is not None:
                logger.warning(msg)
            return None, None, True

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest, follow_symlinks=False)
    # IM4: hash the deployed file (post-copy) rather than trusting the source
    # bundle's expected_hash.  Today the integrator is a raw copy so the
    # values match, but documenting deployed-file provenance now keeps the
    # lockfile honest if future transforms mutate content during deploy.
    file_hash = compute_file_hash(dest)
    if logger:
        logger.verbose_detail(f"deployed {record}")
    return record, file_hash, False
