"""Target-capability warnings emitted during package integration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.remediation import HOOKS_SKIPPED_UNAPPROVED
from apm_cli.integration.targets import (
    target_supports_primitive,
    target_warns_unsupported_primitives,
)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.logging import InstallLogger
    from apm_cli.utils.diagnostics import DiagnosticCollector


def log_hooks_skip(
    package_name: str, package_info: Any, targets: Any, logger: InstallLogger | None
) -> None:
    """Warn about skipped hooks only when the package actually ships them.

    Aligned with :meth:`HookIntegrator.find_hook_files`: checks for
    ``*.json`` in ``.apm/hooks/`` and ``hooks/``.
    """
    _install = Path(package_info.install_path)
    has_hooks = False
    for hook_dir in [_install / ".apm" / "hooks", _install / "hooks"]:
        if hook_dir.is_dir() and any(hook_dir.glob("*.json")):
            has_hooks = True
            break
    if not has_hooks:
        return
    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    if logger:
        from apm_cli.security.trust import approve_command

        approve = HOOKS_SKIPPED_UNAPPROVED.bind(package=_pkg_label).with_command(
            approve_command(_pkg_label)
        )
        logger.warning(
            f"{_pkg_label}: hooks skipped (not approved in executables.allow). "
            f"{approve.sentence()}",
            symbol="warning",
        )


def log_canvas_skip(
    package_name: str,
    package_info: Any,
    logger: InstallLogger | None,
    ctx: InstallContext | None = None,
) -> None:
    """Warn about skipped canvas extensions when the package ships them.

    A hard-denied canvas is named as denied (#4052); approving cannot lift it.
    """
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
        from apm_cli.install.exec_gate import executable_skip_reason
        from apm_cli.security.executables import EXEC_TYPE_CANVAS

        reason = executable_skip_reason(
            ctx, package_name, _pkg_label, EXEC_TYPE_CANVAS, "canvas extension(s)"
        )
        logger.warning(f"{_pkg_label}: {reason}", symbol="warning")


def warn_unsupported_primitives(
    package_info: Any,
    package_name: str,
    targets: Any,
    ctx: InstallContext | None,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None,
) -> None:
    """Warn once for profiles that intentionally omit package primitives."""
    limited_targets = [target for target in targets if target_warns_unsupported_primitives(target)]
    if not limited_targets or ctx is None or ctx.cowork_nonsupported_warned:
        return
    apm_dir = Path(package_info.install_path) / ".apm"
    primitive_dirs = {
        "agents": "agents",
        "prompts": "prompts",
        "instructions": "instructions",
        "hooks": "hooks",
    }
    for target in limited_targets:
        found_types = [
            primitive
            for primitive, subdir in primitive_dirs.items()
            if not target_supports_primitive(target, primitive)
            and (apm_dir / subdir).is_dir()
            and any((apm_dir / subdir).iterdir())
        ]
        if not found_types:
            continue
        package_label = package_name or getattr(package_info, "name", "unknown")
        types_text = ", ".join(sorted(set(found_types)))
        message = (
            f"{target.name} target does not support these primitives; "
            f"{package_label} ({types_text}) will not deploy them"
        )
        if logger:
            logger.warning(message, symbol="warning")
        diagnostics.warn(message)
        ctx.cowork_nonsupported_warned = True
