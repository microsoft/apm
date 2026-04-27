"""Configuration management for APM."""

import json
import os
from typing import Optional  # noqa: F401

CONFIG_DIR = os.path.expanduser("~/.apm")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

_config_cache: dict | None = None


def ensure_config_exists():
    """Ensure the configuration directory and file exist."""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump({"default_client": "vscode"}, f)


def get_config():
    """Get the current configuration.

    Results are cached for the lifetime of the process.

    Returns:
        dict: Current configuration.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    ensure_config_exists()
    with open(CONFIG_FILE) as f:
        _config_cache = json.load(f)
    return _config_cache


def _invalidate_config_cache():
    """Invalidate the config cache (called after writes)."""
    global _config_cache
    _config_cache = None


def update_config(updates):
    """Update the configuration with new values.

    Args:
        updates (dict): Dictionary of configuration values to update.
    """
    _invalidate_config_cache()
    config = get_config()
    config.update(updates)

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    _invalidate_config_cache()


def get_default_client():
    """Get the default MCP client.

    Returns:
        str: Default MCP client type.
    """
    return get_config().get("default_client", "vscode")


def set_default_client(client_type):
    """Set the default MCP client.

    Args:
        client_type (str): Type of client to set as default.
    """
    update_config({"default_client": client_type})


def get_auto_integrate() -> bool:
    """Get the auto-integrate setting.

    Returns:
        bool: Whether auto-integration is enabled (default: True).
    """
    return get_config().get("auto_integrate", True)


def set_auto_integrate(enabled: bool) -> None:
    """Set the auto-integrate setting.

    Args:
        enabled: Whether to enable auto-integration.
    """
    update_config({"auto_integrate": enabled})


def get_temp_dir() -> str | None:
    """Get the configured temporary directory.

    Returns:
        The stored temp_dir config value, or None if not set.
    """
    return get_config().get("temp_dir")


def set_temp_dir(path: str) -> None:
    """Set the temporary directory after validating it exists and is writable.

    The path is normalised (``~`` expansion + absolute) before validation and
    storage so that relative or home-relative paths work predictably.

    Args:
        path: Filesystem path to use as temporary directory.

    Raises:
        ValueError: If the path does not exist, is not a directory, or is not
            writable.
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(resolved):
        raise ValueError(f"Directory does not exist: {resolved}")
    if not os.path.isdir(resolved):
        raise ValueError(f"Path is not a directory: {resolved}")
    if not os.access(resolved, os.W_OK):
        raise ValueError(f"Directory is not writable: {resolved}")
    update_config({"temp_dir": resolved})


def unset_temp_dir() -> None:
    """Remove the ``temp_dir`` key from the config file.

    No-op if the key is not present.
    """
    _invalidate_config_cache()
    config = get_config()
    if "temp_dir" in config:
        del config["temp_dir"]
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


# ---------------------------------------------------------------------------
# Cowork skills directory
# ---------------------------------------------------------------------------


def get_copilot_cowork_skills_dir() -> str | None:
    """Get the configured cowork skills directory.

    Returns:
        The stored ``copilot_cowork_skills_dir`` config value, or ``None`` if not set.
    """
    return get_config().get("copilot_cowork_skills_dir")


def set_copilot_cowork_skills_dir(path: str) -> None:
    """Set the cowork skills directory after validation.

    The path is expanded (``~``) and verified to be absolute.  The
    directory does **not** need to exist on disk (OneDrive may not yet
    be synced).

    Args:
        path: Filesystem path to use as the cowork skills directory.

    Raises:
        ValueError: If *path* is empty, whitespace-only, or relative
            after expansion.
    """
    if not path or not path.strip():
        raise ValueError("Path cannot be empty")
    expanded = os.path.normpath(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        raise ValueError(f"Path must be absolute: {expanded}")
    update_config({"copilot_cowork_skills_dir": expanded})


def unset_copilot_cowork_skills_dir() -> None:
    """Remove the ``copilot_cowork_skills_dir`` key from the config file.

    No-op if the key is not present.
    """
    _invalidate_config_cache()
    config = get_config()
    if "copilot_cowork_skills_dir" in config:
        del config["copilot_cowork_skills_dir"]
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


def get_apm_temp_dir() -> str | None:
    """Return the effective temporary directory for APM operations.

    Resolution order:
      1. ``APM_TEMP_DIR`` environment variable (escape-hatch override)
      2. ``temp_dir`` value from ``~/.apm/config.json``
      3. ``None`` (caller falls back to the system default)

    Empty or whitespace-only values are treated as unset and skipped.

    Returns:
        Directory path string, or None when the system default should be used.
    """
    env_val = os.environ.get("APM_TEMP_DIR", "").strip()
    if env_val:
        return env_val
    config_val = (get_temp_dir() or "").strip()
    if config_val:
        return config_val
    return None
