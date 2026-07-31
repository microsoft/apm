"""APM config command group."""

import builtins
import re
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..version import get_version
from ._config_ops import (
    _get_config_getters,
    _handle_get_external,
    _handle_get_named_key,
    _handle_get_registry,
    _handle_set_external,
    _handle_set_named_key,
    _handle_set_registry,
    _handle_unset_external,
    _handle_unset_registry,
    _unset_dispatch,
)
from ._config_ops import _render_target_value as _render_target_value
from ._config_ops import _require_external_scanners as _require_external_scanners
from ._config_ops import _show_all_user_config as _show_all_user_config
from ._config_ops import _valid_config_keys as _valid_config_keys
from ._config_ops import _validate_scanner_name as _validate_scanner_name
from ._helpers import HIGHLIGHT, RESET, _get_console, _load_apm_config

# Restore builtin since a subcommand is named ``set``
set = builtins.set

_REGISTRY_KEY_RE = re.compile(r"^registry\.([a-zA-Z0-9._-]+)\.(url|token|default)$")
_EXTERNAL_SCANNER_KEY_RE = re.compile(r"^external\.([a-zA-Z0-9._-]+)\.(llm|args)$")


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
    logger = CommandLogger("config set")
    registry_match = _REGISTRY_KEY_RE.match(key)
    if registry_match:
        _handle_set_registry(logger, key, registry_match, value)
        return
    external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
    if external_match:
        _handle_set_external(logger, key, external_match, value)
        return
    _handle_set_named_key(logger, key, value)


@config.command(help="Get a configuration value")
@click.argument("key", required=False)
def get(key):
    """Get a configuration value or show all configuration.

    Examples:
        apm config get auto-integrate
        apm config get
    """
    logger = CommandLogger("config get")
    if key:
        registry_match = _REGISTRY_KEY_RE.match(key)
        if registry_match:
            _handle_get_registry(logger, key, registry_match)
            return
        external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
        if external_match:
            _handle_get_external(logger, key, external_match)
            return
        _handle_get_named_key(logger, key, _get_config_getters())
    else:
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
        _handle_unset_registry(logger, key, registry_match)
        return
    external_match = _EXTERNAL_SCANNER_KEY_RE.match(key)
    if external_match:
        _handle_unset_external(logger, key, external_match)
        return
    _unset_dispatch(logger, key)
