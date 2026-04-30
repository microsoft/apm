"""APM config command group."""

import builtins
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
_CONFIG_KEY_DISPLAY_NAMES = {
    "auto_integrate": "auto-integrate",
    "temp_dir": "temp-dir",
    "copilot_cowork_skills_dir": "copilot-cowork-skills-dir",
}


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
    from ..config import set_auto_integrate

    return {
        "auto-integrate": (set_auto_integrate, "Auto-integration"),
    }


def _get_config_getters():
    """Return config getters keyed by CLI option name."""
    from ..config import get_auto_integrate

    return {
        "auto-integrate": get_auto_integrate,
    }


def _valid_config_keys() -> str:
    """Return valid config keys for messages."""
    return ", ".join(["auto-integrate", "temp-dir", "copilot-cowork-skills-dir"])


@click.group(help="Configure APM CLI", invoke_without_command=True)
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

            from ..config import get_temp_dir as _get_temp_dir

            _temp_dir_val = _get_temp_dir()
            if _temp_dir_val:
                config_table.add_row("", "Temp Directory", _temp_dir_val)

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

            from ..config import get_temp_dir as _get_temp_dir_fb

            _temp_dir_fb = _get_temp_dir_fb()
            if _temp_dir_fb:
                click.echo(f"  Temp Directory: {_temp_dir_fb}")

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
    if enabled:
        logger.success(f"{label} enabled")
    else:
        logger.success(f"{label} disabled")


@config.command(help="Get a configuration value")
@click.argument("key", required=False)
def get(key):
    """Get a configuration value or show all configuration.

    Examples:
        apm config get auto-integrate
        apm config get
    """
    from ..config import get_auto_integrate, get_temp_dir

    logger = CommandLogger("config get")
    getters = _get_config_getters()
    if key:
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

        getter = getters.get(key)
        if getter is None:
            logger.error(f"Unknown configuration key: '{key}'")
            logger.progress(f"Valid keys: {_valid_config_keys()}")
            logger.progress(
                "This error may indicate a bug in command routing. Please report this issue."
            )
            sys.exit(1)
        value = getter()
        click.echo(f"{key}: {value}")
    else:
        # Show all user-settable keys with their effective values (including
        # defaults).  Iterating raw config keys would hide settings that
        # have not been written yet (e.g. auto_integrate on a fresh install).
        logger.progress("APM Configuration:")
        click.echo(f"  auto-integrate: {get_auto_integrate()}")
        temp_dir = get_temp_dir()
        click.echo(
            f"  temp-dir: {temp_dir if temp_dir is not None else 'Not set (using system default)'}"
        )

        from ..core.experimental import is_enabled as _is_enabled_get

        if _is_enabled_get("copilot_cowork"):
            from ..config import get_copilot_cowork_skills_dir as _get_csd_get

            csd = _get_csd_get()
            click.echo(
                f"  copilot-cowork-skills-dir: "
                f"{csd if csd is not None else 'Not set (using auto-detection)'}"
            )


@config.command(help="Unset a configuration value")
@click.argument("key")
def unset(key):
    """Unset (remove) a configuration value.

    Examples:
        apm config unset temp-dir
        apm config unset copilot-cowork-skills-dir
    """
    logger = CommandLogger("config unset")

    if key == "temp-dir":
        from ..config import unset_temp_dir

        unset_temp_dir()
        logger.success("Temporary directory configuration removed")
        return

    if key == "copilot-cowork-skills-dir":
        from ..config import unset_copilot_cowork_skills_dir

        unset_copilot_cowork_skills_dir()
        logger.success("Cowork skills directory configuration removed")
        return

    logger.error(f"Unknown configuration key: '{key}'")
    logger.progress(f"Valid keys: {_valid_config_keys()}")
    sys.exit(1)
