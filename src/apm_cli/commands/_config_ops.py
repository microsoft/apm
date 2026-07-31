"""Private helpers extracted from config.py (800-line guardrail)."""

from __future__ import annotations

import os
import sys

import click

from ..core.command_logger import CommandLogger

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

_BOOLEAN_TRUE_VALUES = {"true", "1", "yes"}
_BOOLEAN_FALSE_VALUES = {"false", "0", "no"}


def _parse_bool_value(value: str) -> bool:
    """Parse a CLI boolean string to bool; raise ValueError on bad input."""
    normalized = value.strip().lower()
    if normalized in _BOOLEAN_TRUE_VALUES:
        return True
    if normalized in _BOOLEAN_FALSE_VALUES:
        return False
    raise ValueError(f"Invalid value '{value}'. Use 'true' or 'false'.")


def _render_target_value(value: str | list[str]) -> str:
    """Render a target value for CLI output."""
    if isinstance(value, list):
        return ",".join(value)
    return value


# ---------------------------------------------------------------------------
# Config key helpers (moved from config.py to break 800-line guardrail)
# ---------------------------------------------------------------------------


def _get_config_setters() -> dict:
    """Return config setters keyed by CLI option name."""
    from ..config import set_allow_protocol_fallback, set_auto_integrate, set_prefer_ssh

    return {
        "auto-integrate": (set_auto_integrate, "Auto-integration"),
        "allow-protocol-fallback": (set_allow_protocol_fallback, "Protocol fallback"),
        "prefer-ssh": (set_prefer_ssh, "SSH transport preference"),
    }


def _get_config_getters() -> dict:
    """Return config getters keyed by CLI option name."""
    from ..config import get_allow_protocol_fallback, get_auto_integrate, get_prefer_ssh

    return {
        "auto-integrate": get_auto_integrate,
        "allow-protocol-fallback": get_allow_protocol_fallback,
        "prefer-ssh": get_prefer_ssh,
    }


def _valid_config_keys() -> str:
    """Return valid config keys for error messages."""
    from ..core.experimental import is_enabled

    keys = [
        "auto-integrate",
        "target",
        "self-update.channel",
        "self-update.install-dir",
        "mcp-registry-url",
        "temp-dir",
        "allow-protocol-fallback",
        "prefer-ssh",
    ]
    if is_enabled("external_scanners"):
        keys.append("audit-on-install")
        keys.append("external.<name>.llm")
        keys.append("external.<name>.args")
    if is_enabled("copilot_cowork"):
        keys.append("copilot-cowork-skills-dir")
    if is_enabled("registries"):
        keys.append("registry.<name>.url")
        keys.append("registry.<name>.token")
        keys.append("registry.<name>.default")
    return ", ".join(keys)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _show_all_user_config(logger: CommandLogger) -> None:
    """Print all user-settable configuration keys with effective values."""
    from ..config import (
        get_allow_protocol_fallback,
        get_auto_integrate,
        get_install_target,
        get_mcp_registry_url,
        get_prefer_ssh,
        get_self_update_channel,
        get_self_update_install_dir,
        get_temp_dir,
    )

    logger.progress("APM Configuration:")
    click.echo(f"  auto-integrate: {str(get_auto_integrate()).lower()}")
    temp_dir = get_temp_dir()
    click.echo(
        f"  temp-dir: {temp_dir if temp_dir is not None else 'Not set (using system default)'}"
    )
    install_target = get_install_target()
    if install_target is not None:
        click.echo(f"  target: {_render_target_value(install_target)}")
    self_update_channel = get_self_update_channel()
    if self_update_channel != "stable":
        click.echo(f"  self-update.channel: {self_update_channel}")
    self_update_install_dir = get_self_update_install_dir()
    if self_update_install_dir is not None:
        click.echo(f"  self-update.install-dir: {self_update_install_dir}")
    if get_allow_protocol_fallback():
        click.echo("  allow-protocol-fallback: true")
    if get_prefer_ssh():
        click.echo("  prefer-ssh: true")

    from ..core.experimental import is_enabled

    if is_enabled("external_scanners"):
        from ..config import get_audit_on_install, get_scanner_options
        from ..security.external.registry import SUPPORTED_SCANNERS

        click.echo(f"  audit-on-install: {get_audit_on_install()}")
        for scanner in SUPPORTED_SCANNERS:
            llm, args = get_scanner_options(scanner)
            if llm is not None:
                click.echo(f"  external.{scanner}.llm: {str(llm).lower()}")
            if args is not None:
                click.echo(f"  external.{scanner}.args: {' '.join(args)}")

    if is_enabled("copilot_cowork"):
        from ..config import get_copilot_cowork_skills_dir

        csd = get_copilot_cowork_skills_dir()
        click.echo(
            f"  copilot-cowork-skills-dir: "
            f"{csd if csd is not None else 'Not set (using auto-detection)'}"
        )

    mcp_url = get_mcp_registry_url()
    click.echo(
        f"  mcp-registry-url: "
        f"{mcp_url if mcp_url is not None else 'Not set (using default https://api.mcp.github.com)'}"
    )


def _require_external_scanners(logger: CommandLogger, key: str) -> None:
    """Exit with an actionable error if the external-scanners flag is off."""
    from ..core.experimental import is_enabled

    if not is_enabled("external_scanners"):
        logger.error(
            f"'{key}' requires the external-scanners experimental flag. "
            "Run: apm experimental enable external-scanners"
        )
        sys.exit(1)


def _validate_scanner_name(logger: CommandLogger, name: str) -> None:
    """Exit with an error if *name* is not a supported external scanner."""
    from ..security.external.registry import SUPPORTED_SCANNERS

    if name not in SUPPORTED_SCANNERS:
        logger.error(
            f"Unknown external scanner '{name}'. Supported: {', '.join(SUPPORTED_SCANNERS)}."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# set sub-command handlers
# ---------------------------------------------------------------------------


def _handle_set_registry(logger: CommandLogger, key: str, registry_match, value: str) -> None:
    """Handle `apm config set registry.<name>.<field>` keys."""
    from ..core.experimental import is_enabled

    if not is_enabled("registries"):
        logger.error(
            f"'{key}' requires the registries experimental flag. "
            "Run: apm experimental enable registries"
        )
        sys.exit(1)
    reg_name, field = registry_match.group(1), registry_match.group(2)
    from ..config import set_registry_default, set_registry_token, set_registry_url

    if field == "url":
        set_registry_url(reg_name, value)
        logger.success(f"registry.{reg_name}.url set")
    elif field == "token":
        set_registry_token(reg_name, value)
        logger.success(f"registry.{reg_name}.token set")
    else:
        try:
            is_default = _parse_bool_value(value)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        set_registry_default(reg_name, is_default)
        logger.success(f"registry.{reg_name}.default {'set' if is_default else 'cleared'}")


def _handle_set_external(logger: CommandLogger, key: str, external_match, value: str) -> None:
    """Handle `apm config set external.<scanner>.<field>` keys."""
    import shlex

    scanner_name, field = external_match.group(1), external_match.group(2)
    _require_external_scanners(logger, key)
    _validate_scanner_name(logger, scanner_name)
    from ..config import set_scanner_args, set_scanner_llm

    if field == "llm":
        try:
            llm = _parse_bool_value(value)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        set_scanner_llm(scanner_name, llm)
        logger.success(f"external.{scanner_name}.llm set to {'true' if llm else 'false'}")
    else:
        try:
            tokens = shlex.split(value, posix=(os.name != "nt"))
        except ValueError as exc:
            logger.error(f"Could not parse args value: {exc}")
            sys.exit(1)
        if not tokens:
            logger.error("external.<name>.args requires at least one token.")
            sys.exit(1)
        set_scanner_args(scanner_name, tokens)
        logger.success(f"external.{scanner_name}.args set ({len(tokens)} token(s))")


def _handle_set_named_key(logger: CommandLogger, key: str, value: str) -> None:
    """Handle `apm config set` for all non-regex named keys."""
    from ..config import get_temp_dir, set_temp_dir

    if key == "copilot-cowork-skills-dir":
        from ..core.experimental import is_enabled

        if not is_enabled("copilot_cowork"):
            logger.error(
                "copilot-cowork-skills-dir requires the copilot-cowork experimental flag. "
                "Run: apm experimental enable copilot-cowork"
            )
            sys.exit(1)
        from ..config import get_copilot_cowork_skills_dir, set_copilot_cowork_skills_dir

        try:
            set_copilot_cowork_skills_dir(value)
            logger.success(f"Cowork skills directory set to: {get_copilot_cowork_skills_dir()}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key == "temp-dir":
        try:
            set_temp_dir(value)
            logger.success(f"Temporary directory set to: {get_temp_dir()}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key == "target":
        from ..config import set_install_target

        try:
            parsed = set_install_target(value)
            logger.success(f"Default install target set to: {_render_target_value(parsed)}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key == "self-update.channel":
        from ..config import set_self_update_channel

        try:
            channel = set_self_update_channel(value)
            logger.success(f"Self-update channel saved: {channel}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key == "self-update.install-dir":
        from ..config import set_self_update_install_dir

        try:
            install_dir = set_self_update_install_dir(value)
            logger.success(f"Self-update install directory saved: {install_dir}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key.startswith("self-update."):
        logger.error(
            "self-update config only supports non-secret installer preferences: "
            "self-update.channel, self-update.install-dir"
        )
        logger.info(
            "Credentials, tokens, mirror URLs, commands, and installer args are not persisted."
        )
        sys.exit(1)

    if key == "mcp-registry-url":
        from ..config import get_mcp_registry_url, set_mcp_registry_url

        try:
            set_mcp_registry_url(value)
            logger.success(f"MCP registry URL set to: {get_mcp_registry_url()}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    if key == "audit-on-install":
        from ..core.experimental import is_enabled

        if not is_enabled("external_scanners"):
            logger.error(
                "audit-on-install requires the external-scanners experimental flag. "
                "Run: apm experimental enable external-scanners"
            )
            sys.exit(1)
        from ..config import get_audit_on_install, set_audit_on_install

        try:
            set_audit_on_install(value)
            logger.success(f"Install-time audit set to: {get_audit_on_install()}")
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)
        return

    setters = _get_config_setters()
    config_entry = setters.get(key)
    if config_entry is None:
        logger.error(f"Unknown configuration key: '{key}'")
        logger.progress(f"Valid keys: {_valid_config_keys()}")
        logger.progress(
            "This error may indicate a bug in command routing. Please report this issue."
        )
        sys.exit(1)
    try:
        enabled = _parse_bool_value(value)
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)
    setter, label = config_entry
    setter(enabled)
    logger.success(f"{label} set to {'true' if enabled else 'false'}")
    if key == "allow-protocol-fallback" and enabled and os.environ.get("CI"):
        logger.warning(
            "allow-protocol-fallback is now persisted to ~/.apm/config.json. "
            "In CI environments with a shared $HOME this will affect all subsequent "
            "apm install runs on this host. "
            "Prefer APM_ALLOW_PROTOCOL_FALLBACK=1 as an invocation-scoped alternative."
        )


# ---------------------------------------------------------------------------
# get sub-command handlers
# ---------------------------------------------------------------------------


def _handle_get_registry(logger: CommandLogger, key: str, registry_match) -> None:
    """Handle `apm config get registry.<name>.<field>` keys."""
    from ..core.experimental import is_enabled

    if not is_enabled("registries"):
        logger.error(
            f"'{key}' requires the registries experimental flag. "
            "Run: apm experimental enable registries"
        )
        sys.exit(1)
    reg_name, field = registry_match.group(1), registry_match.group(2)
    from ..config import get_registry_config, is_registry_default

    cfg = get_registry_config(reg_name)
    if field == "default":
        val = is_registry_default(reg_name)
        click.echo(f"{key}: {'true' if val else 'false'}")
        return
    val = (cfg or {}).get(field)
    if val is None:
        click.echo(f"{key}: Not set")
    else:
        click.echo(f"{key}: {val}")


def _handle_get_external(logger: CommandLogger, key: str, external_match) -> None:
    """Handle `apm config get external.<scanner>.<field>` keys."""
    scanner_name, field = external_match.group(1), external_match.group(2)
    _require_external_scanners(logger, key)
    _validate_scanner_name(logger, scanner_name)
    from ..config import get_scanner_options

    llm, args = get_scanner_options(scanner_name)
    if field == "llm":
        if llm is None:
            click.echo(f"{key}: Not set")
        else:
            click.echo(f"{key}: {str(llm).lower()}")
    elif args is None:
        click.echo(f"{key}: Not set")
    else:
        click.echo(f"{key}: {' '.join(args)}")


def _get_named_key_value(key: str) -> str | None:
    """Resolve a known named config key to its display string, or None."""
    if key == "copilot-cowork-skills-dir":
        from ..config import get_copilot_cowork_skills_dir

        v = get_copilot_cowork_skills_dir()
        return v if v is not None else "Not set (using auto-detection)"
    if key == "temp-dir":
        from ..config import get_temp_dir

        v = get_temp_dir()
        return v if v is not None else "Not set (using system default)"
    if key == "target":
        from ..config import get_install_target

        v = get_install_target()
        return _render_target_value(v) if v is not None else "Not set (using auto-detection)"
    if key == "self-update.channel":
        from ..config import get_self_update_channel

        return str(get_self_update_channel())
    if key == "self-update.install-dir":
        from ..config import get_self_update_install_dir

        v = get_self_update_install_dir()
        return v if v is not None else "Not set (using installer default)"
    if key == "mcp-registry-url":
        from ..config import get_mcp_registry_url

        v = get_mcp_registry_url()
        return v if v is not None else "Not set (using default https://api.mcp.github.com)"
    if key == "audit-on-install":
        from ..config import get_audit_on_install

        return str(get_audit_on_install())
    return None


def _handle_get_named_key(logger: CommandLogger, key: str, getters: dict) -> None:
    """Handle `apm config get` for simple named keys and generic bool getters."""
    value = _get_named_key_value(key)
    if value is not None:
        click.echo(f"{key}: {value}")
        return
    getter = getters.get(key)
    if getter is None:
        logger.error(f"Unknown configuration key: '{key}'")
        logger.progress(f"Valid keys: {_valid_config_keys()}")
        logger.progress(
            "This error may indicate a bug in command routing. Please report this issue."
        )
        sys.exit(1)
    v = getter()
    if isinstance(v, bool):
        click.echo(f"{key}: {str(v).lower()}")
    else:
        click.echo(f"{key}: {v}")


# ---------------------------------------------------------------------------
# unset sub-command handlers
# ---------------------------------------------------------------------------


def _handle_unset_registry(logger: CommandLogger, key: str, registry_match) -> None:
    """Handle `apm config unset registry.<name>.<field>` keys."""
    from ..core.experimental import is_enabled

    if not is_enabled("registries"):
        logger.error(
            f"'{key}' requires the registries experimental flag. "
            "Run: apm experimental enable registries"
        )
        sys.exit(1)
    reg_name, field = registry_match.group(1), registry_match.group(2)
    from ..config import set_registry_default, unset_registry_token, unset_registry_url

    if field == "url":
        unset_registry_url(reg_name)
        logger.success(f"registry.{reg_name}.url removed")
    elif field == "token":
        unset_registry_token(reg_name)
        logger.success(f"registry.{reg_name}.token removed")
    else:
        set_registry_default(reg_name, False)
        logger.success(f"registry.{reg_name}.default removed")


def _handle_unset_external(logger: CommandLogger, key: str, external_match) -> None:
    """Handle `apm config unset external.<scanner>.<field>` keys."""
    scanner_name, field = external_match.group(1), external_match.group(2)
    _require_external_scanners(logger, key)
    _validate_scanner_name(logger, scanner_name)
    from ..config import unset_scanner_args, unset_scanner_llm

    if field == "llm":
        unset_scanner_llm(scanner_name)
        logger.success(f"external.{scanner_name}.llm removed")
    else:
        unset_scanner_args(scanner_name)
        logger.success(f"external.{scanner_name}.args removed")


def _unset_dispatch(logger: CommandLogger, key: str) -> None:
    """Dispatch `apm config unset` for named keys; sys.exit on unknown key."""
    from ..config import (
        unset_allow_protocol_fallback,
        unset_audit_on_install,
        unset_copilot_cowork_skills_dir,
        unset_install_target,
        unset_mcp_registry_url,
        unset_prefer_ssh,
        unset_self_update_channel,
        unset_self_update_install_dir,
        unset_temp_dir,
    )

    _simple: dict[str, tuple] = {
        "temp-dir": (unset_temp_dir, "Temporary directory configuration removed"),
        "target": (
            unset_install_target,
            "Default install target removed (will fall back to auto-detection)",
        ),
        "self-update.channel": (
            unset_self_update_channel,
            "Self-update channel removed (defaults to stable)",
        ),
        "self-update.install-dir": (
            unset_self_update_install_dir,
            "Self-update install directory removed (will use installer default)",
        ),
        "mcp-registry-url": (
            unset_mcp_registry_url,
            "MCP registry URL configuration removed (will use env var or default)",
        ),
        "audit-on-install": (
            unset_audit_on_install,
            "Install-time audit configuration removed (defaults to off)",
        ),
        "allow-protocol-fallback": (
            unset_allow_protocol_fallback,
            "Protocol fallback preference removed (will use env var or default)",
        ),
        "prefer-ssh": (
            unset_prefer_ssh,
            "SSH transport preference removed (will use env var or default)",
        ),
        "copilot-cowork-skills-dir": (
            unset_copilot_cowork_skills_dir,
            "Cowork skills directory configuration removed",
        ),
    }

    entry = _simple.get(key)
    if entry is not None:
        fn, msg = entry
        fn()
        logger.success(msg)
        return

    logger.error(f"Unknown configuration key: '{key}'")
    logger.progress(f"Valid keys: {_valid_config_keys()}")
    sys.exit(1)
