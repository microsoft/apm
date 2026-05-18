"""Local bundle (tarball / directory) integration pipeline.

Provides ``integrate_local_bundle`` and its private helpers.
The public symbol is re-exported from ``apm_cli.install.services`` so
all existing import paths continue to work.
"""

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

# Defensive builtins aliases (see __init__ module-level comment).
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass
class LocalBundleOpts:
    """Optional arguments for :func:`integrate_local_bundle`."""

    diagnostics: Any = None
    logger: Any = None
    scope: Any = None
    alias: str | None = None
    force: bool = False
    dry_run: bool = False


from ._local_deploy_helpers import (  # noqa: E402
    _compute_bundle_record,
    _deploy_file,
    _DeployFlags,
    _stage_instruction_dest,
)


@dataclass(frozen=True, slots=True)
class _BundleDeployCtx:
    """Immutable per-deploy context for local-bundle helpers."""

    pack_files: Any
    bundle_dir: Any
    slug: Any
    project_root: Any
    scope: Any
    flags: _DeployFlags


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_pack_files(bundle_info: Any, bundle_dir: Path) -> dict[str, str]:
    """Build the ``{rel_path: sha256hex}`` mapping for all bundle files.

    Reads from ``bundle_info.lockfile["pack"]["bundle_files"]`` when
    available; falls back to a recursive directory walk.  In both cases
    ``plugin.json`` and ``.mcp.json`` are filtered out (they are bundle
    metadata, never deployable in consumer projects).

    Returns an empty dict when the bundle has no deployable files.
    """
    pack_files: dict[str, str] = {}
    if bundle_info.lockfile:
        pack = bundle_info.lockfile.get("pack") or {}
        bf = pack.get("bundle_files") or {}
        if isinstance(bf, dict):
            pack_files = {str(k): str(v) for k, v in bf.items()}

    if not pack_files:
        # Fallback: walk bundle and hash everything except apm.lock.yaml
        # and plugin.json / .mcp.json.  Prevents zero-deploy when an older
        # bundle without bundle_files lands.
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            if rel == "apm.lock.yaml" or rel.lower() == "plugin.json" or rel.lower() == ".mcp.json":
                continue
            pack_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    # py-arch-2: Filter bundle-metadata files (plugin.json, .mcp.json) out of
    # pack_files BEFORE the per-target loop.  Case-insensitive match mirrors
    # the fallback walk above and the previously-inline guards in the deploy
    # loop.
    filtered: dict[str, str] = {}
    for _rel, _hash in pack_files.items():
        if _rel.lower() in {"plugin.json", ".mcp.json"}:
            continue
        filtered[_rel] = _hash
    return filtered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _prepare_target_roots(target, project_root: Path) -> tuple[Path, dict]:
    """Return ``(default_deploy_root, primitive_roots)`` for *target*."""
    resolved_root = getattr(target, "resolved_deploy_root", None)
    if resolved_root is not None:
        default_deploy_root = Path(resolved_root)
    else:
        default_deploy_root = project_root / target.root_dir
    _primitive_roots: builtins.dict[str, Path] = {}
    for prim_name, prim_mapping in (target.primitives or {}).items():
        if getattr(prim_mapping, "deploy_root", None) and resolved_root is None:
            _primitive_roots[prim_name] = project_root / prim_mapping.deploy_root
    return default_deploy_root, _primitive_roots


def _resolve_dest(
    rel: str,
    bundle_ctx: _BundleDeployCtx,
    target,
    default_deploy_root: Path,
    primitive_roots: dict,
) -> tuple[Path, Path] | None:
    """Resolve ``(dest, deploy_root)`` for *rel* within *target*.

    Returns ``None`` when the path is unsafe or when instruction staging
    fails.  The caller should increment *skipped* and ``continue``.
    """
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    _first_seg = rel.split("/", 1)[0] if "/" in rel else ""
    if _first_seg == "instructions" and "instructions" not in (target.primitives or {}):
        _stage = _stage_instruction_dest(
            rel, bundle_ctx.slug, bundle_ctx.project_root, bundle_ctx.flags.logger
        )
        if _stage is None:
            return None
        dest, deploy_root = _stage
    else:
        deploy_root = primitive_roots.get(_first_seg, default_deploy_root)
        dest = deploy_root / rel
    try:
        ensure_path_within(dest, deploy_root)
    except PathTraversalError as exc:
        if bundle_ctx.flags.logger is not None:
            bundle_ctx.flags.logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
        return None
    return dest, deploy_root


def _deploy_for_target(
    target,
    bundle_ctx: _BundleDeployCtx,
) -> tuple[builtins.list, builtins.dict, int]:
    """Deploy all *bundle_ctx.pack_files* for *target*.

    Returns ``(deployed_files, deployed_hashes, skipped)`` for this target.
    """
    from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

    default_deploy_root, _primitive_roots = _prepare_target_roots(target, bundle_ctx.project_root)
    deployed_files: builtins.list[str] = []
    deployed_hashes: builtins.dict[str, str] = {}
    skipped = 0
    for rel, expected_hash in sorted(bundle_ctx.pack_files.items()):
        try:
            validate_path_segments(str(rel), context="bundle_files key")
        except PathTraversalError as exc:
            if bundle_ctx.flags.logger is not None:
                bundle_ctx.flags.logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
            skipped += 1
            continue
        src = bundle_ctx.bundle_dir / rel
        if not src.is_file() or src.is_symlink():
            skipped += 1
            continue
        result = _resolve_dest(rel, bundle_ctx, target, default_deploy_root, _primitive_roots)
        if result is None:
            skipped += 1
            continue
        dest, _deploy_root = result
        record = _compute_bundle_record(dest, bundle_ctx.project_root, bundle_ctx.scope)
        deployed_record, file_hash, was_skipped = _deploy_file(
            src, dest, record, expected_hash, bundle_ctx.flags
        )
        if was_skipped:
            skipped += 1
        elif deployed_record:
            deployed_files.append(deployed_record)
            deployed_hashes[deployed_record] = file_hash  # type: ignore[assignment]
    return deployed_files, deployed_hashes, skipped


def integrate_local_bundle(
    bundle_info: Any,
    project_root: Path,
    *,
    targets: Any,
    opts: LocalBundleOpts | None = None,
    **kwargs,
) -> dict:
    """Integrate a detected local bundle into project / user scope.

    Local bundles are produced by ``apm pack`` and shipped (via shared file,
    USB, etc.) to environments that cannot reach the source registry.  This
    orchestrator deploys the bundle's plugin-format files into each active
    target's deploy root and returns a result dict so the caller can persist
    ``local_deployed_files`` / ``local_deployed_file_hashes`` into the
    project lockfile.

    The bundle is treated as a *synthetic* package -- its slug derives from
    *alias* (``--as``) when provided, else from ``bundle_info.package_id``.

    Important contract: this function does **NOT** mutate ``apm.yml``.  Local
    bundles are imperative deploys, not declarative dependencies.

    Args:
        bundle_info: ``LocalBundleInfo`` describing the verified bundle.
        project_root: Workspace root (or ``Path.home()`` for ``--global``).
        targets: Resolved ``TargetProfile`` instances from
            ``resolve_targets()``.
        force: When ``True``, overwrite locally-modified files on collision.
        dry_run: When ``True``, report what would be deployed without
            writing to disk.
        opts: Optional :class:`LocalBundleOpts` for diagnostics, logger,
            scope, and alias.

    Returns:
        Dict with keys ``deployed_files`` (list[str]),
        ``deployed_file_hashes`` (dict[str, str]), ``skipped`` (int), and
        per-primitive counters (``skills``, ``agents``, ``commands``, ...).
    """
    _opts = opts or LocalBundleOpts(
        force=kwargs.get("force", False),
        dry_run=kwargs.get("dry_run", False),
        diagnostics=kwargs.get("diagnostics"),
        logger=kwargs.get("logger"),
        scope=kwargs.get("scope"),
        alias=kwargs.get("alias"),
    )
    logger = _opts.logger
    alias = _opts.alias

    bundle_dir: Path = bundle_info.source_dir
    pack_files = _build_pack_files(bundle_info, bundle_dir)

    if not pack_files:
        return {
            "deployed_files": [],
            "deployed_file_hashes": {},
            "skipped": 0,
            "skills": 0,
            "agents": 0,
            "commands": 0,
            "hooks": 0,
            "instructions": 0,
            "prompts": 0,
            "sub_skills": 0,
        }

    slug = alias or bundle_info.package_id
    if logger:
        logger.verbose_detail(
            f"Integrating local bundle '{slug}' "
            f"({len(pack_files)} file(s), targets={[t.name for t in targets]})"
        )

    # NOTE(M-arch-1): Local bundles intentionally do NOT route through
    # ``integrate_package_primitives`` -- they are an imperative deploy of
    # opaque files keyed by ``pack.bundle_files`` rather than a primitive
    # tree.  Revisit when local-bundle install needs to share collision /
    # link-resolution logic with the dependency-resolver pipeline.
    _flags = _DeployFlags(
        force=_opts.force,
        dry_run=_opts.dry_run,
        diagnostics=_opts.diagnostics,
        logger=logger,
    )
    _bundle_ctx = _BundleDeployCtx(
        pack_files=pack_files,
        bundle_dir=bundle_dir,
        slug=slug,
        project_root=project_root,
        scope=_opts.scope,
        flags=_flags,
    )
    deployed_files: builtins.list[str] = []
    deployed_hashes: builtins.dict[str, str] = {}
    skipped = 0

    for target in targets:
        _t_files, _t_hashes, _t_skipped = _deploy_for_target(target, _bundle_ctx)
        deployed_files.extend(_t_files)
        deployed_hashes.update(_t_hashes)
        skipped += _t_skipped

    return {
        "deployed_files": deployed_files,
        "deployed_file_hashes": deployed_hashes,
        "skipped": skipped,
        "skills": 0,
        "agents": 0,
        "commands": 0,
        "hooks": 0,
        "instructions": 0,
        "prompts": 0,
        "sub_skills": 0,
    }


__all__ = ["integrate_local_bundle"]
