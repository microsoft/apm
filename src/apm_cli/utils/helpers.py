"""Helper utility functions for APM."""

import platform
from pathlib import Path


def detect_platform():
    """Detect the current platform.

    Returns:
        str: Platform name (macos, linux, windows).
    """
    system = platform.system().lower()

    if system == "darwin":  # noqa: SIM116
        return "macos"
    elif system == "linux":
        return "linux"
    elif system == "windows":
        return "windows"
    else:
        return "unknown"


def find_plugin_json(plugin_path: Path, *, include_codex: bool = False) -> Path | None:
    """Find plugin.json in a plugin directory.

    Checks spec-defined locations in priority order:
      1. <root>/plugin.json
      2. <root>/.github/plugin/plugin.json
      3. <root>/.claude-plugin/plugin.json
      4. <root>/.cursor-plugin/plugin.json
      5. <root>/.codex-plugin/plugin.json (native consumer opt-in only)

    Args:
        plugin_path: Path to the plugin directory

    Returns:
        Optional[Path]: Path to the plugin.json file if found, None otherwise
    """
    candidates = [
        plugin_path / "plugin.json",
        plugin_path / ".github" / "plugin" / "plugin.json",
        plugin_path / ".claude-plugin" / "plugin.json",
        plugin_path / ".cursor-plugin" / "plugin.json",
    ]
    if include_codex:
        from apm_cli.codex_plugins.manifest import CODEX_COMPAT_MANIFEST

        candidates.append(plugin_path / CODEX_COMPAT_MANIFEST)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
