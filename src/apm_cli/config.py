"""Configuration management for APM."""

import json
import os
from typing import Optional  # noqa: F401

# ---------------------------------------------------------------------------
# Public env-var names (re-declared here to avoid a circular import with the
# transport_selection module which also defines them).
# ---------------------------------------------------------------------------
_ENV_ALLOW_PROTOCOL_FALLBACK = "APM_ALLOW_PROTOCOL_FALLBACK"
_ENV_GIT_PROTOCOL = "APM_GIT_PROTOCOL"

CONFIG_DIR = os.path.expanduser("~/.apm")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

_config_cache: dict | None = None


def ensure_config_exists():
    """Ensure the configuration directory and file exist."""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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
    with open(CONFIG_FILE, encoding="utf-8") as f:
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

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


# ---------------------------------------------------------------------------
# Protocol transport preferences (issue #1243)
# ---------------------------------------------------------------------------


def get_allow_protocol_fallback() -> bool:
    """Get the allow-protocol-fallback setting.

    Returns:
        bool: Whether cross-protocol fallback is enabled (default: False).
    """
    return get_config().get("allow_protocol_fallback", False)


def set_allow_protocol_fallback(enabled: bool) -> None:
    """Set the allow-protocol-fallback setting.

    Args:
        enabled: Whether to enable cross-protocol fallback.
    """
    update_config({"allow_protocol_fallback": enabled})


def get_prefer_ssh() -> bool:
    """Get the prefer-ssh transport preference setting.

    Returns:
        bool: Whether SSH is preferred for shorthand dependencies (default: False).
    """
    return get_config().get("prefer_ssh", False)


def set_prefer_ssh(enabled: bool) -> None:
    """Set the prefer-ssh transport preference setting.

    Args:
        enabled: Whether to prefer SSH for shorthand (owner/repo) dependencies.
    """
    update_config({"prefer_ssh": enabled})


def unset_allow_protocol_fallback() -> None:
    """Remove the ``allow_protocol_fallback`` key from the config file.

    No-op if the key is not present.  After this call
    :func:`get_apm_allow_protocol_fallback` will fall through to
    ``APM_ALLOW_PROTOCOL_FALLBACK`` env var and then the built-in
    default (``False``).
    """
    _invalidate_config_cache()
    config = get_config()
    if "allow_protocol_fallback" in config:
        del config["allow_protocol_fallback"]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


def unset_prefer_ssh() -> None:
    """Remove the ``prefer_ssh`` key from the config file.

    No-op if the key is not present.  After this call
    :func:`get_apm_protocol_pref` will fall through to the
    ``APM_GIT_PROTOCOL`` env var and then the built-in default (``None``).
    """
    _invalidate_config_cache()
    config = get_config()
    if "prefer_ssh" in config:
        del config["prefer_ssh"]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


def _parse_allow_protocol_fallback_env(raw: str | None) -> bool | None:
    """Parse ``APM_ALLOW_PROTOCOL_FALLBACK`` as a tri-state value.

    Args:
        raw: Raw environment variable value, or ``None`` when unset.

    Returns:
        ``True`` for explicit truthy values (``1``, ``true``, ``yes``, ``on``),
        ``False`` for explicit falsy values (``0``, ``false``, ``no``, ``off``),
        or ``None`` when the variable is unset, empty, or unrecognised.
    """
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized == "":
        return None
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return None


def get_apm_allow_protocol_fallback() -> bool:
    """Return the effective allow-protocol-fallback flag.

    Resolution order:
      1. ``APM_ALLOW_PROTOCOL_FALLBACK`` environment variable
         (``"1"``/``"true"``/``"yes"``/``"on"`` => True;
          ``"0"``/``"false"``/``"no"``/``"off"`` => False)
      2. ``allow_protocol_fallback`` value from ``~/.apm/config.json``
      3. ``False`` (default)

    Returns:
        ``True`` when cross-protocol fallback is enabled, otherwise ``False``.
    """
    env_value = _parse_allow_protocol_fallback_env(os.environ.get(_ENV_ALLOW_PROTOCOL_FALLBACK))
    if env_value is not None:
        return env_value
    return get_allow_protocol_fallback()


def get_apm_protocol_pref() -> str | None:
    """Return the effective protocol preference string.

    Resolution order:
      1. ``APM_GIT_PROTOCOL`` environment variable
         (``"ssh"``, ``"https"``, or ``"http"`` — ``"http"`` is treated
         as an alias for ``"https"`` by the transport selector)
      2. ``prefer_ssh`` boolean in ``~/.apm/config.json`` (maps to ``"ssh"`` when True)
      3. ``None`` (let the transport selector use git insteadOf rules)

    Returns:
        ``"ssh"``, ``"https"``, ``"http"``, or ``None``.
    """
    env_val = os.environ.get(_ENV_GIT_PROTOCOL, "").strip().lower()
    if env_val in ("ssh", "https", "http"):
        return env_val
    if get_prefer_ssh():
        return "ssh"
    return None


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
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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
