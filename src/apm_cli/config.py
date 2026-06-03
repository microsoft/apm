"""Configuration management for APM."""

import contextlib
import json
import os

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
    """Ensure the configuration directory and file exist.

    The directory is created with mode ``0o700`` (owner-only) and the
    initial config file with mode ``0o600`` to prevent other users on a
    shared system from reading persisted tokens or transport preferences.
    Both restrictions are silently ignored on Windows.
    """
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        try:
            fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"default_client": "vscode"}, f)
        except FileExistsError:
            pass
        with contextlib.suppress(NotImplementedError, OSError):
            os.chmod(CONFIG_FILE, 0o600)


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


def _unset_config_key(key: str) -> None:
    """Remove *key* from the config file atomically.

    No-op when *key* is not present.  Invalidates the in-process cache
    before and after the write so subsequent reads see the updated state.

    Args:
        key: The JSON key to remove from ``~/.apm/config.json``.
    """
    _invalidate_config_cache()
    config = get_config()
    if key in config:
        del config[key]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    _invalidate_config_cache()


def unset_temp_dir() -> None:
    """Remove the ``temp_dir`` key from the config file.

    No-op if the key is not present.
    """
    _unset_config_key("temp_dir")


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
    _unset_config_key("allow_protocol_fallback")


def unset_prefer_ssh() -> None:
    """Remove the ``prefer_ssh`` key from the config file.

    No-op if the key is not present.  After this call
    :func:`get_apm_protocol_pref` will fall through to the
    ``APM_GIT_PROTOCOL`` env var and then the built-in default (``None``).
    """
    _unset_config_key("prefer_ssh")


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
    _unset_config_key("copilot_cowork_skills_dir")


def _get_registries_section() -> dict:
    """Return the ``registries`` section from config.json as a dict."""
    regs = get_config().get("registries", {})
    return regs if isinstance(regs, dict) else {}


def get_registry_config(name: str) -> "dict | None":
    """Return the config.json entry for registry *name*, or None."""
    entry = _get_registries_section().get(name)
    return entry if isinstance(entry, dict) else None


def set_registry_url(name: str, url: str) -> None:
    """Write registry.<name>.url to config.json."""
    regs = dict(_get_registries_section())
    entry = dict(regs.get(name) or {})
    entry["url"] = url
    regs[name] = entry
    update_config({"registries": regs})


def set_registry_token(name: str, token: str) -> None:
    """Write registry.<name>.token to config.json."""
    regs = dict(_get_registries_section())
    entry = dict(regs.get(name) or {})
    entry["token"] = token
    regs[name] = entry
    update_config({"registries": regs})


def unset_registry_url(name: str) -> None:
    """Remove registry.<name>.url from config.json."""
    regs = dict(_get_registries_section())
    entry = dict(regs.get(name) or {})
    entry.pop("url", None)
    if entry:
        regs[name] = entry
    else:
        regs.pop(name, None)
    update_config({"registries": regs})


def unset_registry_token(name: str) -> None:
    """Remove registry.<name>.token from config.json."""
    regs = dict(_get_registries_section())
    entry = dict(regs.get(name) or {})
    entry.pop("token", None)
    if entry:
        regs[name] = entry
    else:
        regs.pop(name, None)
    update_config({"registries": regs})


def unset_registry(name: str) -> None:
    """Remove the entire registry.<name> entry from config.json."""
    regs = dict(_get_registries_section())
    if name in regs:
        del regs[name]
        update_config({"registries": regs})


def get_config_json_default_registry() -> str | None:
    """Return the registry name marked ``default: true`` in config.json."""
    found: str | None = None
    for name, body in _get_registries_section().items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(body, dict):
            continue
        if body.get("default") is True:
            found = name.strip()
    return found


def set_registry_default(name: str, is_default: bool) -> None:
    """Mark *name* as the user-scoped default registry in config.json."""
    regs = dict(_get_registries_section())
    if is_default:
        for reg_name, body in list(regs.items()):
            if not isinstance(body, dict):
                continue
            if reg_name == name:
                continue
            if body.pop("default", None) is not None:
                regs[reg_name] = body if body else regs[reg_name]
        entry = dict(regs.get(name) or {})
        entry["default"] = True
        regs[name] = entry
    else:
        entry = dict(regs.get(name) or {})
        entry.pop("default", None)
        if entry:
            regs[name] = entry
        else:
            regs.pop(name, None)
    update_config({"registries": regs})


def is_registry_default(name: str) -> bool:
    """Return whether *name* is marked as the config.json default registry."""
    cfg = get_registry_config(name)
    return bool(cfg and cfg.get("default") is True)


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


# ---------------------------------------------------------------------------
# Install-time audit default (external_scanners experimental feature)
# ---------------------------------------------------------------------------

#: Valid modes for the install-time audit. ``off`` skips it; ``warn`` runs the
#: audit and surfaces findings without failing; ``block`` fails the install on
#: critical findings.
AUDIT_ON_INSTALL_MODES = ("off", "warn", "block")


def get_audit_on_install() -> str:
    """Get the user-level default mode for running ``apm audit`` at install time.

    This is only consulted when the ``external_scanners`` experimental flag is
    enabled and an ``apm-policy.yml`` ``security.audit.on_install`` rule does
    not already mandate a stricter mode.

    Returns:
        One of :data:`AUDIT_ON_INSTALL_MODES`; defaults to ``"off"``.
    """
    value = get_config().get("audit_on_install", "off")
    return value if value in AUDIT_ON_INSTALL_MODES else "off"


def set_audit_on_install(mode: str) -> None:
    """Set the install-time audit default mode.

    Args:
        mode: One of :data:`AUDIT_ON_INSTALL_MODES`.

    Raises:
        ValueError: If *mode* is not a recognised value.
    """
    normalized = (mode or "").strip().lower()
    if normalized not in AUDIT_ON_INSTALL_MODES:
        raise ValueError(
            f"Invalid value '{mode}'. Use one of: {', '.join(AUDIT_ON_INSTALL_MODES)}."
        )
    update_config({"audit_on_install": normalized})


def unset_audit_on_install() -> None:
    """Remove the ``audit_on_install`` key from the config file.

    No-op if the key is not present.  After this call
    :func:`get_audit_on_install` falls through to the built-in default
    (``"off"``).
    """
    _unset_config_key("audit_on_install")


# ---------------------------------------------------------------------------
# External scanner options (external_scanners experimental feature)
# ---------------------------------------------------------------------------
#
# Stored under the ``external_scanners`` JSON section, mirroring the
# ``registries`` nested-dict shape:
#
#     {"external_scanners": {"skillspector": {"llm": true,
#                                             "args": ["--model", "gpt-4o"]}}}
#
# Only consulted when the ``external_scanners`` experimental flag is enabled
# and an external scan actually runs. ``llm`` opts into a scanner's LLM mode;
# ``args`` are extra argv tokens (allowlist-validated by the adapter before
# use). The user-facing config key is ``external.<name>.{llm,args}``; the JSON
# section name stays ``external_scanners`` (internal, like ``registries``).


def _get_external_scanners_section() -> dict:
    """Return the ``external_scanners`` section from config.json as a dict."""
    section = get_config().get("external_scanners", {})
    return section if isinstance(section, dict) else {}


def get_scanner_config(name: str) -> "dict | None":
    """Return the config.json entry for external scanner *name*, or None."""
    entry = _get_external_scanners_section().get(name)
    return entry if isinstance(entry, dict) else None


def set_scanner_llm(name: str, llm: bool) -> None:
    """Write external_scanners.<name>.llm to config.json."""
    scanners = dict(_get_external_scanners_section())
    entry = dict(scanners.get(name) or {})
    entry["llm"] = bool(llm)
    scanners[name] = entry
    update_config({"external_scanners": scanners})


def set_scanner_args(name: str, args: "list[str]") -> None:
    """Write external_scanners.<name>.args to config.json as a list."""
    scanners = dict(_get_external_scanners_section())
    entry = dict(scanners.get(name) or {})
    entry["args"] = list(args)
    scanners[name] = entry
    update_config({"external_scanners": scanners})


def unset_scanner_llm(name: str) -> None:
    """Remove external_scanners.<name>.llm from config.json."""
    _unset_scanner_field(name, "llm")


def unset_scanner_args(name: str) -> None:
    """Remove external_scanners.<name>.args from config.json."""
    _unset_scanner_field(name, "args")


def _unset_scanner_field(name: str, field: str) -> None:
    """Remove one field from external_scanners.<name>, pruning empties."""
    scanners = dict(_get_external_scanners_section())
    entry = dict(scanners.get(name) or {})
    entry.pop(field, None)
    if entry:
        scanners[name] = entry
    else:
        scanners.pop(name, None)
    update_config({"external_scanners": scanners})


def unset_scanner(name: str) -> None:
    """Remove the entire external_scanners.<name> entry from config.json."""
    scanners = dict(_get_external_scanners_section())
    if name in scanners:
        del scanners[name]
        update_config({"external_scanners": scanners})


def get_scanner_options(name: str) -> "tuple[bool | None, tuple[str, ...] | None]":
    """Return ``(llm, args)`` configured for scanner *name*.

    ``llm`` is ``None`` when unset (no opinion); ``args`` is ``None`` when
    unset (distinct from an explicitly empty list).
    """
    entry = get_scanner_config(name)
    if not entry:
        return None, None
    llm = entry.get("llm")
    llm = bool(llm) if isinstance(llm, bool) else None
    raw_args = entry.get("args")
    args = tuple(str(a) for a in raw_args) if isinstance(raw_args, list) else None
    return llm, args


# ---------------------------------------------------------------------------
# MCP registry URL (issue #818)
# ---------------------------------------------------------------------------

_MCP_REGISTRY_URL_KEY = "mcp_registry_url"
_MCP_REGISTRY_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MCP_REGISTRY_URL_MAX_LENGTH = 2048


def _validate_mcp_registry_url(url: str) -> str:
    """Validate and normalise a registry URL.  Returns the trimmed URL.

    Raises:
        ValueError: If the URL is empty, too long, missing a scheme/host,
            or uses a scheme outside ``http``/``https``.
    """
    from urllib.parse import urlparse

    normalized = url.strip().rstrip("/")
    if not normalized:
        raise ValueError("mcp-registry-url: URL cannot be empty")
    if len(normalized) > _MCP_REGISTRY_URL_MAX_LENGTH:
        raise ValueError(
            f"mcp-registry-url: URL is too long "
            f"({len(normalized)} > {_MCP_REGISTRY_URL_MAX_LENGTH} characters)"
        )
    parsed = urlparse(normalized)
    if not parsed.scheme:
        raise ValueError(
            f"mcp-registry-url: Invalid URL '{normalized}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    scheme = parsed.scheme.lower()
    if scheme not in _MCP_REGISTRY_ALLOWED_SCHEMES:
        raise ValueError(
            f"mcp-registry-url: scheme '{scheme}' is not supported; "
            f"use http:// or https://. "
            f"WebSocket URLs (ws/wss) and file:// paths are rejected for security."
        )
    if parsed.username is not None:
        raise ValueError(
            "mcp-registry-url: URL must not contain credentials; "
            "use the MCP_REGISTRY_URL environment variable or a credential helper instead."
        )
    if not parsed.hostname:
        raise ValueError(
            f"mcp-registry-url: Invalid URL '{normalized}': expected scheme://host "
            f"(e.g. https://mcp.internal.example.com)"
        )
    return normalized


def get_mcp_registry_url() -> str | None:
    """Return the user-configured MCP registry URL, or None if not set."""
    return get_config().get(_MCP_REGISTRY_URL_KEY)


def set_mcp_registry_url(url: str) -> None:
    """Persist *url* as the user-scope MCP registry URL.

    Args:
        url: Registry URL (``http://`` or ``https://`` only).

    Raises:
        ValueError: If the URL is invalid.
    """
    normalized = _validate_mcp_registry_url(url)
    update_config({_MCP_REGISTRY_URL_KEY: normalized})


def unset_mcp_registry_url() -> None:
    """Remove the ``mcp_registry_url`` key from the config file.

    No-op if the key is not present.
    """
    _unset_config_key(_MCP_REGISTRY_URL_KEY)
