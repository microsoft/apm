"""Pure helper functions extracted from ``install/services.py``.

Canvas-trust wiring and local-bundle deploy helpers live here so
``integrate_local_bundle`` stays within the Stage-2 complexity budget.
This module is a leaf: it does NOT import from services.py at module scope.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..install.diagnostics import DiagnosticCollector


def _label_and_deploy_dir(
    prim_name: str, mapping: Any, target: Any, deploy_dir: str
) -> tuple[str, str]:
    """Return ``(label, deploy_dir)`` for a per-kind integration line."""
    if prim_name == "instructions" and mapping.output_compare:
        # Rule-dir formats (cursor/claude/windsurf) are the output_compare
        # set; derive the label from the same flag so a new rule format
        # needs no edit here.
        return "rule(s)", deploy_dir
    if prim_name == "instructions":
        return "instruction(s)", deploy_dir
    if prim_name == "hooks":
        if target.hooks_config_display:
            deploy_dir = target.hooks_config_display
        return "hook(s)", deploy_dir
    if prim_name == "canvas":
        return "canvas extension(s)", deploy_dir
    return prim_name, deploy_dir


def _emit_integration_hints(prim_name: str, info: dict, log_integration: Any) -> None:
    """Emit per-primitive 'next step' hints after an integration line."""
    if any(p.startswith("copilot-app/") for p in info["paths"]) and info["files"] > 0:
        log_integration(
            "  |-- workflows arrive disabled; enable from the Copilot App's Workflows tab"
        )
    if prim_name == "canvas" and (info["files"] > 0 or info["adopted"] > 0):
        log_integration("  |-- reload the Copilot session (/clear) or restart to load the canvas")


def _resolve_package_key(package_info: Any, package_name: str) -> str:
    """Delegate to ``exec_gate.resolve_package_key``."""
    from apm_cli.install.exec_gate import resolve_package_key

    return resolve_package_key(package_info, package_name)


def _log_hooks_skip(
    package_name: str, package_info: Any, targets: Any, logger: InstallLogger | None
) -> None:
    """Warn about skipped hooks only when the package actually ships them."""
    del targets
    install_path = Path(package_info.install_path)
    has_hooks = False
    for hook_dir in [install_path / ".apm" / "hooks", install_path / "hooks"]:
        if hook_dir.is_dir() and any(hook_dir.glob("*.json")):
            has_hooks = True
            break
    if not has_hooks:
        return
    pkg_label = package_name or getattr(package_info, "name", "unknown")
    if logger:
        logger.warning(
            f"{pkg_label}: hooks skipped (not approved in allowExecutables). "
            f"Run 'apm approve {pkg_label}' to approve.",
            symbol="warning",
        )


def _check_executable_approval(
    package_name: str,
    package_info: Any,
    allow_executables: dict[str, dict[str, bool]] | None,
    *,
    ctx: Any = None,
) -> tuple[bool, bool, bool, bool]:
    """Delegate to ``exec_gate.check_executable_approval``."""
    from apm_cli.install.exec_gate import check_executable_approval

    return check_executable_approval(package_name, package_info, allow_executables, ctx=ctx)


def _log_canvas_skip(package_name: str, package_info: Any, logger: InstallLogger | None) -> None:
    """Warn about skipped canvas extensions when the package ships them."""
    _install = Path(package_info.install_path)
    extensions_root = _install / ".apm" / "extensions"
    try:
        has_canvas = extensions_root.is_dir() and any(
            (d / "extension.mjs").is_file() for d in extensions_root.iterdir() if d.is_dir()
        )
    except OSError:
        has_canvas = False
    if not has_canvas:
        return
    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    if logger:
        logger.warning(
            f"{_pkg_label}: canvas extension(s) skipped (not approved in allowExecutables). "
            f"Run 'apm approve {_pkg_label}' to approve.",
            symbol="warning",
        )


def _load_bundle_pack_files(bundle_info: Any) -> dict[str, str]:
    """Load and filter the file manifest for a local bundle.

    Reads ``pack.bundle_files`` from the bundle lockfile when present;
    falls back to walking the bundle directory.  Either way, strips
    bundle-metadata entries (plugin.json, .mcp.json, apm.lock.yaml).
    """
    bundle_dir: Path = bundle_info.source_dir
    pack_files: dict[str, str] = {}
    if bundle_info.lockfile:
        pack = bundle_info.lockfile.get("pack") or {}
        bf = pack.get("bundle_files") or {}
        if isinstance(bf, dict):
            pack_files = {str(k): str(v) for k, v in bf.items()}

    if not pack_files:
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            lower = rel.lower()
            if rel == "apm.lock.yaml" or lower in {"plugin.json", ".mcp.json"}:
                continue
            pack_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    return {r: h for r, h in pack_files.items() if r.lower() not in {"plugin.json", ".mcp.json"}}


def _apply_canvas_gate(
    pack_files: dict[str, str],
    slug: str,
    allow_executables: dict[str, dict[str, bool]] | None,
    *,
    logger: InstallLogger | None,
    diagnostics: DiagnosticCollector | None,
) -> tuple[dict[str, str], int]:
    """Filter canvas extension files from *pack_files* per feature + approval gate.

    Returns ``(filtered_pack_files, extra_skipped)`` where *extra_skipped* is
    non-zero only when canvas is enabled but the bundle is NOT approved -- in
    that case the caller's skipped counter should be incremented.  When canvas
    is disabled the files are silently dropped without counting as skipped.
    """
    from ..core.experimental import is_enabled
    from ..integration.canvas_integrator import is_canvas_bundle_path

    _canvas_enabled = is_enabled("canvas")
    if _canvas_enabled:
        from ..security.executables import EXEC_TYPE_CANVAS, is_package_approved

        _canvas_approved = allow_executables is None or is_package_approved(
            allow_executables, slug, EXEC_TYPE_CANVAS
        )
    else:
        _canvas_approved = False

    if _canvas_enabled and _canvas_approved:
        return pack_files, 0

    _blocked = sorted(r for r in pack_files if is_canvas_bundle_path(r))
    filtered = {k: v for k, v in pack_files.items() if k not in _blocked}
    if not _blocked:
        return filtered, 0

    if not _canvas_enabled:
        # Canvas feature is off -- drop silently, do NOT count as skipped.
        return filtered, 0

    # Canvas is on but not approved: warn and count.
    _msg = (
        f"Blocked {len(_blocked)} canvas extension file(s) from bundle "
        f"'{slug}': canvas extensions are executable extension.mjs code "
        f"and are not approved in allowExecutables. "
        f"Run 'apm approve {slug}' to approve them."
    )
    if diagnostics is not None:
        diagnostics.warn(message=_msg, package=str(slug))
    elif logger is not None:
        logger.warning(_msg)
    return filtered, len(_blocked)


def _resolve_bundle_file_dest(
    rel: str,
    target: Any,
    default_deploy_root: Path,
    project_root: Path,
    slug: str,
    primitive_roots: dict[str, Path],
    *,
    logger: InstallLogger | None,
) -> tuple[Path, Path] | None:
    """Return ``(dest, deploy_root)`` for one bundle file in one target.

    Returns ``None`` when the file should be skipped for this target
    (non-Copilot canvas extension, invalid slug for compile-only staging,
    or path traversal in stage root).  The caller must handle
    ``PathTraversalError`` on *dest* itself -- this function only resolves
    where the file would go and handles the instructions-staging branch.
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within

    _rel_norm = rel.replace("\\", "/")
    _first_seg = _rel_norm.split("/", 1)[0] if "/" in _rel_norm else ""

    if _first_seg.lower() == "extensions" and target.name != "copilot":
        return None

    if _first_seg == "instructions" and "instructions" not in (target.primitives or {}):
        # Compile-only target: stage under apm_modules/<slug>/.apm/instructions/
        from .services_integrate import _validate_bundle_slug

        _slug_str = str(slug)
        if not _validate_bundle_slug(_slug_str, logger):
            return None
        stage_root = project_root / "apm_modules" / slug / ".apm" / "instructions"
        try:
            ensure_path_within(stage_root, project_root / "apm_modules")
        except PathTraversalError as exc:
            if logger is not None:
                logger.warning(f"Skipped unsafe stage root for {slug!r}: {exc}")
            return None
        _rel_under = rel.split("/", 1)[1] if "/" in rel else Path(rel).name
        return stage_root / _rel_under, stage_root

    deploy_root = primitive_roots.get(_first_seg, default_deploy_root)
    return deploy_root / rel, deploy_root
