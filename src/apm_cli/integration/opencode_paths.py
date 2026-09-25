"""OpenCode user-scope configuration paths.

This module is the canonical owner of OpenCode configuration-directory
environment-variable resolution.
"""

import os
from pathlib import Path


def opencode_user_config_dir() -> Path:
    """Return OpenCode's normalized user configuration directory.

    ``OPENCODE_CONFIG_DIR`` and ``XDG_CONFIG_HOME`` are configuration paths,
    not relative paths rooted at the current working directory.  Relative
    values are ignored and resolution falls through to the next candidate.
    """
    configured = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if configured and Path(configured).expanduser().is_absolute():
        return Path(configured).expanduser().resolve(strict=False)

    xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_home and Path(xdg_home).expanduser().is_absolute():
        return (Path(xdg_home).expanduser() / "opencode").resolve(strict=False)

    return (Path.home() / ".config" / "opencode").resolve(strict=False)
