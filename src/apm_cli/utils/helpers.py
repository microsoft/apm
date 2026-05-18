"""Helper utility functions for APM."""

import platform
import shutil
import subprocess
import sys
from pathlib import Path


def is_tool_available(tool_name):
    """Check if a command-line tool is available.

    Args:
        tool_name (str): Name of the tool to check.

    Returns:
        bool: True if the tool is available, False otherwise.
    """
    # First try using shutil.which which is more reliable across platforms
    if shutil.which(tool_name):
        return True

    # Fall back to subprocess approach if shutil.which returns None
    try:
        # Different approaches for different platforms
        if sys.platform == "win32":
            # On Windows, use 'where' command but WITHOUT shell=True
            result = subprocess.run(
                ["where", tool_name],
                capture_output=True,
                shell=False,  # Changed from True to False
                check=False,
            )
            return result.returncode == 0
        else:
            # On Unix-like systems, use 'which' command
            result = subprocess.run(["which", tool_name], capture_output=True, check=False)
            return result.returncode == 0
    except Exception:
        return False


def get_available_package_managers():
    """Get available package managers on the system.

    Returns:
        dict: Dictionary of available package managers and their paths.
    """
    package_managers = {}

    _collect_python_package_managers(package_managers)
    _collect_javascript_package_managers(package_managers)
    _collect_system_package_managers(package_managers)

    return package_managers


def _collect_python_package_managers(managers: dict) -> None:
    """Collect Python package managers into the given dictionary."""
    if is_tool_available("uv"):
        managers["uv"] = "uv"
    if is_tool_available("pip"):
        managers["pip"] = "pip"
    if is_tool_available("pipx"):
        managers["pipx"] = "pipx"


def _collect_javascript_package_managers(managers: dict) -> None:
    """Collect JavaScript package managers into the given dictionary."""
    if is_tool_available("npm"):
        managers["npm"] = "npm"
    if is_tool_available("yarn"):
        managers["yarn"] = "yarn"
    if is_tool_available("pnpm"):
        managers["pnpm"] = "pnpm"


def _collect_system_package_managers(managers: dict) -> None:
    """Collect system package managers into the given dictionary."""
    if is_tool_available("brew"):
        managers["brew"] = "brew"
    if is_tool_available("apt"):
        managers["apt"] = "apt"
    if is_tool_available("yum"):
        managers["yum"] = "yum"
    if is_tool_available("dnf"):
        managers["dnf"] = "dnf"
    if is_tool_available("apk"):
        managers["apk"] = "apk"
    if is_tool_available("pacman"):
        managers["pacman"] = "pacman"


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


def find_plugin_json(plugin_path: Path) -> Path | None:
    """Find plugin.json in a plugin directory.

    Checks spec-defined locations in priority order:
      1. <root>/plugin.json
      2. <root>/.github/plugin/plugin.json
      3. <root>/.claude-plugin/plugin.json
      4. <root>/.cursor-plugin/plugin.json

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
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
