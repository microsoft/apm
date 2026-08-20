"""Target-capability warnings emitted during package integration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.integration.targets import (
    target_supports_primitive,
    target_warns_unsupported_primitives,
)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.logging import InstallLogger
    from apm_cli.utils.diagnostics import DiagnosticCollector


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
