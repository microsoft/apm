"""Cause-specific diagnostics for unresolved hook command paths."""

from __future__ import annotations

from apm_cli.integration.hook_command_paths import residual_plugin_root_has_path
from apm_cli.utils.console import _rich_warning
from apm_cli.utils.diagnostics import printable_ascii_text


def warn_unresolved_plugin_root(command: str, reference: str, package_name: str) -> None:
    """Warn with remediation matched to the unresolved reference shape."""
    package_label = printable_ascii_text(package_name)
    safe_reference = printable_ascii_text(reference)
    if not residual_plugin_root_has_path(command, reference):
        _rich_warning(
            f"Plugin-root reference has no path in package '{package_label}': "
            f"{safe_reference}. Add a package-relative path after the token, "
            "or remove the token if no package file is needed."
        )
        return
    _rich_warning(
        f"Unresolved plugin-root reference in package '{package_label}': "
        f"{safe_reference}. Ensure the quote marks match and wrap the complete "
        "reference and path in balanced double quotes, then run apm install again."
    )
