"""Helper utility functions for APM."""

import os  # noqa: F401
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional  # noqa: F401


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
            result = subprocess.run(  # noqa: UP022
                ["where", tool_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,  # Changed from True to False
                check=False,
            )
            return result.returncode == 0
        else:
            # On Unix-like systems, use 'which' command
            result = subprocess.run(  # noqa: UP022
                ["which", tool_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )
            return result.returncode == 0
    except Exception:
        return False


def get_available_package_managers():
    """Get available package managers on the system.

    Returns:
        dict: Dictionary of available package managers and their paths.
    """
    package_managers = {}

    # Check for Python package managers
    if is_tool_available("uv"):
        package_managers["uv"] = "uv"
    if is_tool_available("pip"):
        package_managers["pip"] = "pip"
    if is_tool_available("pipx"):
        package_managers["pipx"] = "pipx"

    # Check for JavaScript package managers
    if is_tool_available("npm"):
        package_managers["npm"] = "npm"
    if is_tool_available("yarn"):
        package_managers["yarn"] = "yarn"
    if is_tool_available("pnpm"):
        package_managers["pnpm"] = "pnpm"

    # Check for system package managers
    if is_tool_available("brew"):  # macOS
        package_managers["brew"] = "brew"
    if is_tool_available("apt"):  # Debian/Ubuntu
        package_managers["apt"] = "apt"
    if is_tool_available("yum"):  # CentOS/RHEL
        package_managers["yum"] = "yum"
    if is_tool_available("dnf"):  # Fedora
        package_managers["dnf"] = "dnf"
    if is_tool_available("apk"):  # Alpine
        package_managers["apk"] = "apk"
    if is_tool_available("pacman"):  # Arch
        package_managers["pacman"] = "pacman"

    return package_managers


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
