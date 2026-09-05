"""Path-scanning helpers for the drift replay engine."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile


def _governed_root_dirs(targets: list[TargetProfile]) -> set[str]:
    """Return the set of top-level managed directory names to walk.

    Includes each target's top-level ``root_dir`` (plus ``.apm``) AND every
    per-primitive ``deploy_root`` override (e.g. the ``copilot`` target routing
    ``skills`` to ``.agents``). Walking the deploy roots is what lets the drift
    differ compare committed skill bundles under ``.agents/skills/`` against the
    replay, closing the gap where deployed skill content could silently diverge
    from source (issue #1716). The replay reproduces the deploy-time link
    rewrite faithfully, so byte-identical skills do not surface as false drift.
    Only the first path segment is kept so nested deploy roots collapse to a
    single walk root.
    """
    roots: set[str] = {".apm"}
    for t in targets or []:
        root = getattr(t, "root_dir", None)
        if root:
            roots.add(str(root).split("/", 1)[0])
        primitives = getattr(t, "primitives", None) or {}
        for mapping in primitives.values():
            deploy_root = getattr(mapping, "deploy_root", None)
            if deploy_root:
                roots.add(str(deploy_root).split("/", 1)[0])
    return roots


def _walk_managed(root: Path, governed_roots: set[str]) -> dict[str, Path]:
    """Return a mapping of project-relative posix paths to absolute paths."""
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for top in governed_roots:
        base = root / top
        if not base.exists():
            continue
        if base.is_file() and not base.is_symlink():
            out[top] = base
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(root).as_posix()
                out[rel] = p
    # AGENTS.md is a flat top-level file in some target layouts.
    agents_md = root / "AGENTS.md"
    if agents_md.is_file() and not agents_md.is_symlink():
        out["AGENTS.md"] = agents_md
    return out


def _collect_tracked_files(lockfile: LockFile) -> dict[str, str]:
    """Return scanner membership claims as ``{path: package_owner}``."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile)


def _claimed_prefixes(
    tracked: dict[str, str],
    hashed_files: set[str],
    project_files: dict[str, Path],
) -> tuple[str, ...]:
    """Return ``dir/`` prefixes for the tracked entries that name directories.

    A manifest entry may name a directory rather than each file beneath it
    (``scan_lockfile_packages`` and ``_check_deployed_files_present`` both
    accept that shape), so a file is claimed when a tracked *directory*
    covers it.

    Entries with a recorded hash are files even if the current project path was
    replaced by a directory. Entries present in *project_files* are also files,
    which preserves the same rule for legacy hashless lockfiles. A file claim
    must never act as a prefix: otherwise replacing tracked ``a.md`` with an
    ``a.md/`` directory would let every descendant dodge the membership check.
    """
    return tuple(
        normalized + "/"
        for path in tracked
        if (normalized := path.rstrip("/"))
        and normalized not in hashed_files
        and normalized not in project_files
    )


def _collect_hashed_files(lockfile: LockFile) -> set[str]:
    """Return every deployed path whose lock claim is explicitly file-shaped."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return set(DeploymentLedgerCodec.legacy_deployed_file_hash_paths(lockfile))


def _canvas_deploy_prefixes(targets) -> set[str]:
    """Return ``root/subdir/`` prefixes for every target carrying a canvas mapping.

    Used to exclude canvas extension deploy paths from drift comparison
    (the replay deliberately does not re-integrate canvases).
    """
    prefixes: set[str] = set()
    for target in targets or []:
        mapping = getattr(target, "primitives", {}).get("canvas")
        if mapping is None:
            continue
        effective_root = mapping.deploy_root or target.root_dir
        if mapping.subdir:
            prefixes.add(f"{effective_root}/{mapping.subdir}/")
        else:
            prefixes.add(f"{effective_root}/")
    return prefixes
