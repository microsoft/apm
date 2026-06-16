"""Pure helper functions extracted from ``install/services.py``.

None of these touch any security gate or canvas-trust wiring -- those stay
in services.py adjacent to their call sites.  This module is a leaf: it does
NOT import from services.py at module scope (only via lazy function-local
imports where strictly necessary to avoid circular dependencies).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger


def _deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the lockfile-safe path string for a deployed file."""

    def _try_dynamic_root(tgts: Any, *, strict: bool = False) -> str | None:
        for _t in tgts:
            if _t.resolved_deploy_root is None:
                continue
            if not strict:
                try:
                    target_path.relative_to(_t.resolved_deploy_root)
                except ValueError:
                    continue
            if _t.name == "copilot-app":
                from apm_cli.integration.copilot_app_db import to_lockfile_uri

                return to_lockfile_uri(target_path.name)
            from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

            return to_lockfile_path(target_path, _t.resolved_deploy_root)
        return None

    if targets:
        result = _try_dynamic_root(targets)
        if result is not None:
            return result
    try:
        return target_path.relative_to(project_root).as_posix()
    except ValueError:
        # Fallback: let to_lockfile_path run its own security
        # validation (PathTraversalError) without pre-filtering.
        if targets:
            result = _try_dynamic_root(targets, strict=True)
            if result is not None:
                return result
        raise RuntimeError(  # noqa: B904
            f"Cannot translate {target_path!r} to a lockfile path: "
            f"path is outside the project tree and no dynamic-root "
            f"target matched. This is a bug -- please report it."
        )


def _skill_bundle_file_entries(
    skill_dir: Path,
    project_root: Path,
    targets: Any,
) -> list[str]:
    """Expand a deployed skill directory into per-file lockfile entries."""
    try:
        if not (skill_dir.is_dir() and not skill_dir.is_symlink()):
            return []
    except OSError:
        return []
    entries: list[str] = []
    for bundle_file in sorted(skill_dir.rglob("*")):
        try:
            if bundle_file.is_file() and not bundle_file.is_symlink():
                entries.append(_deployed_path_entry(bundle_file, project_root, targets))
        except OSError:
            continue
    return entries


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
