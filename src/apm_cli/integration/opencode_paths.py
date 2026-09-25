"""OpenCode user-scope configuration paths."""

import os
from pathlib import Path


def opencode_user_config_dir() -> Path:
    """Return OpenCode's normalized user configuration directory."""
    configured = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)

    xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_home:
        return (Path(xdg_home).expanduser() / "opencode").resolve(strict=False)

    return (Path.home() / ".config" / "opencode").resolve(strict=False)
