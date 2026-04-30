"""Version management for APM CLI."""

import sys
from pathlib import Path

# Build-time constants (will be injected during build)
# This avoids TOML parsing overhead during runtime
__BUILD_VERSION__ = None
__BUILD_SHA__ = None


def get_version() -> str:
    """
    Get the current version efficiently.

    First tries build-time constant, then installed package metadata,
    then falls back to pyproject.toml parsing (for development).

    Returns:
        str: Version string
    """
    # Use build-time constant if available (fastest path - for PyInstaller binaries)
    if __BUILD_VERSION__:
        return __BUILD_VERSION__

    # Try to get version from installed package metadata (for pip installations)
    # Skip this in frozen/PyInstaller environments to avoid import issues
    if not getattr(sys, "frozen", False):
        try:
            # Python 3.8+ has importlib.metadata
            if sys.version_info >= (3, 8):  # noqa: UP036
                from importlib.metadata import PackageNotFoundError, version
            else:
                from importlib_metadata import PackageNotFoundError, version

            return version("apm-cli")
        except (ImportError, PackageNotFoundError):
            pass

    # Fallback to reading from pyproject.toml (for development/source installations)
    try:
        # Handle PyInstaller bundle vs development
        if getattr(sys, "frozen", False):
            # Running in PyInstaller bundle
            pyproject_path = Path(sys._MEIPASS) / "pyproject.toml"
        else:
            # Running in development
            pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"

        if pyproject_path.exists():
            # Simple regex parsing instead of full TOML library
            with open(pyproject_path, encoding="utf-8") as f:
                content = f.read()

            # Look for version = "x.y.z" pattern (including PEP 440 prereleases)
            import re

            match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                version_str = match.group(1)
                # Validate PEP 440 version patterns: x.y.z or x.y.z{a|b|rc}N
                if re.match(r"^\d+\.\d+\.\d+(a\d+|b\d+|rc\d+)?$", version_str):
                    return version_str
    except Exception:
        pass

    return "unknown"


def get_build_sha() -> str:
    """Get the short git commit SHA for the current build.

    Uses the build-time constant when available (shipped binaries),
    otherwise falls back to querying git at runtime (development).
    """
    if __BUILD_SHA__:
        return __BUILD_SHA__

    # Fallback: query git at runtime (development only)
    if not getattr(sys, "frozen", False):
        import subprocess

        try:
            repo_root = Path(__file__).parent.parent.parent
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return ""


# For backward compatibility
__version__ = get_version()
