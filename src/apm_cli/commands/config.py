"""APM config command group."""

import builtins
import os
import re
import sys
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..version import get_version
from ._helpers import HIGHLIGHT, RESET, _get_console, _load_apm_config

# Restore builtin since a subcommand is named ``set``
set = builtins.set

_BOOLEAN_TRUE_VALUES = {"true", "1", "yes"}
_BOOLEAN_FALSE_VALUES = {"false", "0", "no"}
_REGISTRY_KEY_RE = re.compile(r"^registry\.([a-zA-Z0-9._-]+)\.(url|token|default)$")
_EXTERNAL_SCANNER_KEY_RE = re.compile(r"^external\.([a-zA-Z0-9._-]+)\.(llm|args)$")
_CONFIG_KEY_DISPLAY_NAMES = {
    "auto_integrate": "auto-integrate",
    "temp_dir": "temp-dir",
    "copilot_cowork_skills_dir": "copilot-cowork-skills-dir",
    "allow_protocol_fallback": "allow-protocol-fallback",
    "prefer_ssh": "prefer-ssh",
}


def _render_target_value(value: str | list[str]) -> str:
    """Render a target value for CLI output."""
    if isinstance(value, list):
        return ",".join(value)
    return value


def _parse_bool_value(value: str) -> bool:
    """Parse a CLI boolean value."""
    normalized = value.strip().lower()
    if normalized in _BOOLEAN_TRUE_VALUES:
        return True
    if normalized in _BOOLEAN_FALSE_VALUES:
        return False
    raise ValueError(f"Invalid value '{value}'. Use 'true' or 'false'.")


def _get_config_setters():
    """Return config setters keyed by CLI option name."""
    from ..config import set_allow_protocol_fallback, set_auto_integrate, set_prefer_ssh

    return {
        "auto-integrate": (set_auto_integrate, "Auto-integration"),
        "allow-protocol-fallback": (set_allow_protocol_fallback, "Protocol fallback"),
        "prefer-ssh": (set_prefer_ssh, "SSH transport preference"),
    }


def _get_config_getters():
    """Return config getters keyed by CLI option name."""
    from ..config import get_allow_protocol_fallback, get_auto_integrate, get_prefer_ssh

    return {
        "auto-integrate": get_auto_integrate,
        "allow-protocol-fallback": get_allow_protocol_fallback,
        "prefer-ssh": get_prefer_ssh,
    }


def _valid_config_keys() -> str:
    """Return valid config keys for messages."""
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


def _show_all_user_config(logger: CommandLogger) -> None:
    """Print all user-settable configuration keys with effective values."""
    from ..config import (
        get_allow_protocol_fallback,
        get_auto_integrate,
        get_prefer_ssh,
        get_temp_dir,
    )

    logger.progress("APM Configuration:")
    click.echo(f"  auto-integrate: {str(get_auto_integrate()).lower()}")
    temp_dir = get_temp_dir()
    click.echo(
        f"  temp-dir: {temp_dir if temp_dir is not None else 'Not set (using system default)'}"
    )
    from ..config import get_install_target as _get_install_target

    _install_target = _get_install_target()
    if _install_target is not None:
        click.echo(f"  target: {_render_target_value(_install_target)}")
    from ..config import (
        get_self_update_channel as _get_self_update_channel,
    )
    from ..config import (
        get_self_update_install_dir as _get_self_update_install_dir,
    )

    _self_update_channel = _get_self_update_channel()
    if _self_update_channel != "stable":
        click.echo(f"  self-update.channel: {_self_update_channel}")
    _self_update_install_dir = _get_self_update_install_dir()
    if _self_update_install_dir is not None:
        click.echo(f"  self-update.install-dir: {_self_update_install_dir}")
    # Only show transport keys when non-default to reduce noise.
    _apf_val = get_allow_protocol_fallback()
    _ssh_val = get_prefer_ssh()
    if _apf_val:
        click.echo("  allow-protocol-fallback: true")
    if _ssh_val:
        click.echo("  prefer-ssh: true")

    from ..core.experimental import is_enabled as _is_enabled_get

    if _is_enabled_get("external_scanners"):
        from ..config import get_audit_on_install, get_scanner_options
        from ..security.external.registry import SUPPORTED_SCANNERS

        click.echo(f"  audit-on-install: {get_audit_on_install()}")
        for _scanner in SUPPORTED_SCANNERS:
            _llm, _args = get_scanner_options(_scanner)
            if _llm is not None:
                click.echo(f"  external.{_scanner}.llm: {str(_llm).lower()}")
            if _args is not None:
                click.echo(f"  external.{_scanner}.args: {' '.join(_args)}")

    if _is_enabled_get("copilot_cowork"):
        from ..config import get_copilot_cowork_skills_dir as _get_csd_get

        csd = _get_csd_get()
        click.echo(
            f"  copilot-cowork-skills-dir: "
            f"{csd if csd is not None else 'Not set (using auto-detection)'}"
        )

    from ..config import get_mcp_registry_url as _get_mcp_registry_url_all

    mcp_url = _get_mcp_registry_url_all()
    click.echo(
        f"  mcp-registry-url: {mcp_url if mcp_url is not None else 'Not set (using default)'}"
    )


def _require_external_scanners(logger, key: str) -> None:
    """Exit with an actionable error if the external-scanners flag is off."""
    from ..core.experimental import is_enabled

    if not is_enabled("external_scanners"):
        logger.error(
            f"'{key}' requires the external-scanners experimental flag. "
            "Run: apm experimental enable external-scanners"
        )
        sys.exit(1)


def _validate_scanner_name(logger, name: str) -> None:
    """Exit with an error if *name* is not a supported external scanner."""
    from ..security.external.registry import SUPPORTED_SCANNERS

    if name not in SUPPORTED_SCANNERS:
        logger.error(
            f"Unknown external scanner '{name}'. Supported: {', '.join(SUPPORTED_SCANNERS)}."
        )
        sys.exit(1)


@click.group(
    help=(
        "Configure APM CLI. Run with no subcommand to show the merged project "
        "and global configuration."
    ),
    invoke_without_command=True,
)
@click.pass_context
def config(ctx):
    """Configure APM CLI settings."""
    # If no subcommand, show current configuration
    if ctx.invoked_subcommand is None:
        logger = CommandLogger("config")
        try:
            # Lazy import rich table
            from rich.table import Table  # type: ignore

            console = _get_console()
            # Create configuration display
            config_table = Table(
                title="Current APM Configuration",
                show_header=True,
                header_style="bold cyan",
            )
            config_table.add_column("Category", style="bold yellow", min_width=12)
            config_table.add_column("Setting", style="white", min_width=15)
            config_table.add_column("Value", style="cyan")

            # Show apm.yml if in project
            if Path(APM_YML_FILENAME).exists():
                apm_config = _load_apm_config()
                config_table.add_row("Project", "Name", apm_config.get("name", "Unknown"))
                config_table.add_row("", "Version", apm_config.get("version", "Unknown"))
                config_table.add_row("", "Entrypoint", apm_config.get("entrypoint", "None"))
                config_table.add_row(
                    "",
                    "MCP Dependencies",
                    str(len(apm_config.get("dependencies", {}).get("mcp", []))),
                )

                # Show compilation configuration
                compilation_config = apm_config.get("compilation", {})
                if compilation_config:
                    config_table.add_row(
                        "Compilation",
                        "Output",
                        compilation_config.get("output", "AGENTS.md"),
                    )
                    config_table.add_row(
                        "",
                        "Chatmode",
                        compilation_config.get("chatmode", "auto-detect"),
                    )
                    config_table.add_row(
                        "",
                        "Resolve Links",
                        str(compilation_config.get("resolve_links", True)),
                    )
                else:
                    config_table.add_row("Compilation", "Status", "Using defaults (no config)")
            else:
                config_table.add_row("Project", "Status", "Not in an APM project directory")

            config_table.add_row("Global", "APM CLI Version", get_version())

            from ..config import get_allow_protocol_fallback as _get_apf
            from ..config import get_prefer_ssh as _get_prefer_ssh_cfg
            from ..config import get_self_update_channel as _get_self_update_channel_cfg
            from ..config import get_self_update_install_dir as _get_self_update_install_dir_cfg
            from ..config import get_temp_dir as _get_temp_dir

            _temp_dir_val = _get_temp_dir()
            if _temp_dir_val:
                config_table.add_row("", "Temp Directory", _temp_dir_val)
            _self_update_channel_val = _get_self_update_channel_cfg()
            if _self_update_channel_val != "stable":
                config_table.add_row("", "Self-update Channel", _self_update_channel_val)
            _self_update_install_dir_val = _get_self_update_install_dir_cfg()
            if _self_update_install_dir_val:
                config_table.add_row(
                    "",
                    "Self-update Install Dir",
                    _self_update_install_dir_val,
                )

            # Only surface transport keys when they have been enabled -- the
            # false-default rows add noise for users who never configured them.
            _apf = _get_apf()
            _prefer_ssh = _get_prefer_ssh_cfg()
            if _apf:
                config_table.add_row("", "Allow Protocol Fallback", "true")
            if _prefer_ssh:
                config_table.add_row("", "Prefer SSH Transport", "true")

            from ..core.experimental import is_enabled as _is_enabled

            if _is_enabled("copilot_cowork"):
                from ..config import get_copilot_cowork_skills_dir as _get_csd

                _csd_val = _get_csd()
                config_table.add_row(
                    "",
                    "Cowork Skills Dir",
                    _csd_val if _csd_val else "Not set (using auto-detection)",
                )

            console.print(config_table)

        except (ImportError, NameError):
            # Fallback display
            logger.progress("Current APM Configuration:")

            if Path(APM_YML_FILENAME).exists():
                apm_config = _load_apm_config()
                click.echo(f"\n{HIGHLIGHT}Project (apm.yml):{RESET}")
                click.echo(f"  Name: {apm_config.get('name', 'Unknown')}")
                click.echo(f"  Version: {apm_config.get('version', 'Unknown')}")
                click.echo(f"  Entrypoint: {apm_config.get('entrypoint', 'None')}")
                click.echo(
                    f"  MCP Dependencies: {len(apm_config.get('dependencies', {}).get('mcp', []))}"
                )
            else:
                logger.progress("Not in an APM project directory")

            click.echo(f"\n{HIGHLIGHT}Global:{RESET}")
            click.echo(f"  APM CLI Version: {get_version()}")

            from ..config import get_allow_protocol_fallback as _get_apf_fb
            from ..config import get_prefer_ssh as _get_prefer_ssh_fb
            from ..config import get_self_update_channel as _get_self_update_channel_fb
            from ..config import get_self_update_install_dir as _get_self_update_install_dir_fb
            from ..config import get_temp_dir as _get_temp_dir_fb

            _temp_dir_fb = _get_temp_dir_fb()
            if _temp_dir_fb:
                click.echo(f"  Temp Directory: {_temp_dir_fb}")
            _self_update_channel_fb = _get_self_update_channel_fb()
            if _self_update_channel_fb != "stable":
                click.echo(f"  self-update.channel: {_self_update_channel_fb}")
            _self_update_install_dir_fb = _get_self_update_install_dir_fb()
            if _self_update_install_dir_fb:
                click.echo(f"  self-update.install-dir: {_self_update_install_dir_fb}")

            click.echo(f"  allow-protocol-fallback: {str(_get_apf_fb()).lower()}")
            click.echo(f"  prefer-ssh: {str(_get_prefer_ssh_fb()).lower()}")

            from ..core.experimental import is_enabled as _is_enabled_fb

            if _is_enabled_fb("copilot_cowork"):
                from ..config import get_copilot_cowork_skills_dir as _get_csd_fb

                _csd_fb = _get_csd_fb()
                click.echo(
                    f"  Cowork Skills Dir: "
                    f"{_csd_fb if _csd_fb else 'Not set (using auto-detection)'}"
                )


@config.command(help="Set a configuration value")
@click.argument("key")
@click.argument("value")
def set(key, value):  # noqa: F811
    """Set a configuration value.

    Examples:
        apm config set auto-integrate false
        apm config set auto-integrate true
    """
    from ..config import get_temp_dir, set_temp_dir

    logger = CommandLogger("config set")

    registry_match = _REGISTRY_KEY_RE.match(key)
    if registry_match:
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
            if is_default:
                logger.success(f"registry.{reg_name}.default set")
            else:
                logger.success(f"registry.{reg_name}.default cleared")
        return

    external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
    if external_match:
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
            import shlex

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
        return

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
            rendered = _render_target_value(parsed)
            logger.success(f"Default install target set to: {rendered}")
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

    # Warn when persisting allow-protocol-fallback=true in a CI environment where
    # $HOME is often shared across jobs -- the persisted value will affect all
    # subsequent apm install runs on that host. The env var is safer for CI.
    import os as _os

    if key == "allow-protocol-fallback" and enabled and _os.environ.get("CI"):
        logger.warning(
            "allow-protocol-fallback is now persisted to ~/.apm/config.json. "
            "In CI environments with a shared $HOME this will affect all subsequent "
            "apm install runs on this host. "
            "Prefer APM_ALLOW_PROTOCOL_FALLBACK=1 as an invocation-scoped alternative."
        )


@config.command(help="Get a configuration value")
@click.argument("key", required=False)
def get(key):
    """Get a configuration value or show all configuration.

    Examples:
        apm config get auto-integrate
        apm config get
    """
    from ..config import get_temp_dir

    logger = CommandLogger("config get")
    getters = _get_config_getters()
    if key:
        registry_match = _REGISTRY_KEY_RE.match(key)
        if registry_match:
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
            return

        if key == "copilot-cowork-skills-dir":
            from ..config import get_copilot_cowork_skills_dir

            value = get_copilot_cowork_skills_dir()
            if value is None:
                click.echo("copilot-cowork-skills-dir: Not set (using auto-detection)")
            else:
                click.echo(f"copilot-cowork-skills-dir: {value}")
            return

        if key == "temp-dir":
            value = get_temp_dir()
            if value is None:
                click.echo("temp-dir: Not set (using system default)")
            else:
                click.echo(f"temp-dir: {value}")
            return

        if key == "target":
            from ..config import get_install_target

            value = get_install_target()
            if value is None:
                click.echo("target: Not set (using auto-detection)")
            else:
                click.echo(f"target: {_render_target_value(value)}")
            return

        if key == "self-update.channel":
            from ..config import get_self_update_channel

            click.echo(f"self-update.channel: {get_self_update_channel()}")
            return

        if key == "self-update.install-dir":
            from ..config import get_self_update_install_dir

            value = get_self_update_install_dir()
            if value is None:
                click.echo("self-update.install-dir: Not set (using installer default)")
            else:
                click.echo(f"self-update.install-dir: {value}")
            return

        if key == "mcp-registry-url":
            from ..config import get_mcp_registry_url

            value = get_mcp_registry_url()
            if value is None:
                click.echo("mcp-registry-url: Not set (using default https://api.mcp.github.com)")
            else:
                click.echo(f"mcp-registry-url: {value}")
            return

        if key == "audit-on-install":
            from ..config import get_audit_on_install

            click.echo(f"audit-on-install: {get_audit_on_install()}")
            return

        external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
        if external_match:
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
            return

        getter = getters.get(key)
        if getter is None:
            logger.error(f"Unknown configuration key: '{key}'")
            logger.progress(f"Valid keys: {_valid_config_keys()}")
            logger.progress(
                "This error may indicate a bug in command routing. Please report this issue."
            )
            sys.exit(1)
        value = getter()
        # Render booleans as lowercase true/false (npm convention).
        if isinstance(value, bool):
            click.echo(f"{key}: {str(value).lower()}")
        else:
            click.echo(f"{key}: {value}")
    else:
        # Show all user-settable keys with their effective values (including
        # defaults).  Iterating raw config keys would hide settings that
        # have not been written yet (e.g. auto_integrate on a fresh install).
        _show_all_user_config(logger)


@config.command(name="list", help="List all configuration values")
def list_config():
    """List all user-settable configuration values with effective values."""
    _show_all_user_config(CommandLogger("config list"))


@config.command(help="Unset a configuration value")
@click.argument("key")
def unset(key):
    """Unset (remove) a configuration value.

    Examples:
        apm config unset temp-dir
        apm config unset allow-protocol-fallback
        apm config unset prefer-ssh
        apm config unset copilot-cowork-skills-dir
    """
    logger = CommandLogger("config unset")

    registry_match = _REGISTRY_KEY_RE.match(key)
    if registry_match:
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
        return

    if key == "temp-dir":
        from ..config import unset_temp_dir

        unset_temp_dir()
        logger.success("Temporary directory configuration removed")
        return

    if key == "target":
        from ..config import unset_install_target

        unset_install_target()
        logger.success("Default install target removed (will fall back to auto-detection)")
        return

    if key == "self-update.channel":
        from ..config import unset_self_update_channel

        unset_self_update_channel()
        logger.success("Self-update channel removed (defaults to stable)")
        return

    if key == "self-update.install-dir":
        from ..config import unset_self_update_install_dir

        unset_self_update_install_dir()
        logger.success("Self-update install directory removed (will use installer default)")
        return

    if key == "mcp-registry-url":
        from ..config import unset_mcp_registry_url

        unset_mcp_registry_url()
        logger.success("MCP registry URL configuration removed (will use env var or default)")
        return

    if key == "audit-on-install":
        from ..config import unset_audit_on_install

        unset_audit_on_install()
        logger.success("Install-time audit configuration removed (defaults to off)")
        return

    external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
    if external_match:
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
        return

    if key == "allow-protocol-fallback":
        from ..config import unset_allow_protocol_fallback

        unset_allow_protocol_fallback()
        logger.success("Protocol fallback preference removed (will use env var or default)")
        return

    if key == "prefer-ssh":
        from ..config import unset_prefer_ssh

        unset_prefer_ssh()
        logger.success("SSH transport preference removed (will use env var or default)")
        return

    if key == "copilot-cowork-skills-dir":
        from ..config import unset_copilot_cowork_skills_dir

        unset_copilot_cowork_skills_dir()
        logger.success("Cowork skills directory configuration removed")
        return

    logger.error(f"Unknown configuration key: '{key}'")
    logger.progress(f"Valid keys: {_valid_config_keys()}")
    sys.exit(1)
