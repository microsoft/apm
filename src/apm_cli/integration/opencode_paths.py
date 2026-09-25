"""OpenCode user-scope configuration paths.

This module is the canonical owner of OpenCode configuration-directory
environment-variable resolution.
"""

import os
from pathlib import Path


def _opencode_user_config_path(*, resolve: bool) -> Path:
    """Resolve OpenCode's user configuration root with the requested identity.

    ``OPENCODE_CONFIG_DIR`` and ``XDG_CONFIG_HOME`` are configuration paths,
    not relative paths rooted at the current working directory. Relative
    values are ignored and resolution falls through to the next candidate.
    """
    configured = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if configured and Path(configured).expanduser().is_absolute():
        path = Path(configured).expanduser()
        return path.resolve(strict=False) if resolve else path

    xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_home and Path(xdg_home).expanduser().is_absolute():
        path = Path(xdg_home).expanduser() / "opencode"
        return path.resolve(strict=False) if resolve else path

    path = Path.home() / ".config" / "opencode"
    return path.resolve(strict=False) if resolve else path


def opencode_user_config_path() -> Path:
    """Return OpenCode's lexical user configuration root.

    Unlike :func:`opencode_user_config_dir`, this preserves symlink components
    so safety checks can inspect the path before filesystem traversal.
    """
    return _opencode_user_config_path(resolve=False)


def opencode_user_config_dir() -> Path:
    """Return OpenCode's resolved user configuration directory for detection."""
    return _opencode_user_config_path(resolve=True)
