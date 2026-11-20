"""APM config command group."""

import builtins
import os
import re
import sys
from pathlib import Path

import click

from apm_cli.core.remediation import (
    EXPERIMENTAL_FLAG_DISABLED,
)
from apm_cli.core.scope import manifest_file

from ..core.command_logger import CommandLogger
from ..install.locking import serialized_lifecycle
from ..utils.credential_rendering import redact_secrets, render_config_value
from ..version import get_version
from . import self_update as self_update_command
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
    "github_auth_first": "github-auth-first",
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
    raise ValueError(
        f"Invalid value '{render_config_value('value', value)}'. Use 'true' or 'false'."
    )


def _get_config_setters():
    """Return config setters keyed by CLI option name."""
    from ..config import (
        set_allow_protocol_fallback,
        set_auto_integrate,
        set_github_auth_first,
        set_prefer_ssh,
    )

    return {
        "auto-integrate": (set_auto_integrate, "Auto-integration"),
        "allow-protocol-fallback": (set_allow_protocol_fallback, "Protocol fallback"),
        "prefer-ssh": (set_prefer_ssh, "SSH transport preference"),
        "github-auth-first": (set_github_auth_first, "GitHub auth-first"),
    }


def _get_config_getters():
    """Return config getters keyed by CLI option name."""
    from ..config import (
        get_allow_protocol_fallback,
        get_auto_integrate,
        get_github_auth_first,
        get_prefer_ssh,
    )

    return {
        "auto-integrate": get_auto_integrate,
        "allow-protocol-fallback": get_allow_protocol_fallback,
        "prefer-ssh": get_prefer_ssh,
        "github-auth-first": get_github_auth_first,
    }


# Keys still accepted by get/set so existing scripts keep working, but no
# longer advertised. Each maps to the warning shown when it is used.
_DEPRECATED_CONFIG_KEYS = {
    "auto-integrate": (
        "'auto-integrate' is deprecated and has no effect: no command reads it. "
        "It will be removed in a future release."
    ),
}


# Single registry of user-facing config keys: (name, experimental flag, unsettable).
# Messages derive the valid-key list from it; tests pin it to the dispatch below.
_CONFIG_KEYS: tuple[tuple[str, str | None, bool], ...] = (
    ("target", None, True),
    ("self-update.channel", None, True),
    ("self-update.install-dir", None, True),
    ("mcp-registry-url", None, True),
    ("temp-dir", None, True),
    ("allow-protocol-fallback", None, True),
    ("prefer-ssh", None, True),
    ("github-auth-first", None, True),
    ("audit-on-install", "external_scanners", True),
    ("external.<name>.llm", "external_scanners", True),
    ("external.<name>.args", "external_scanners", True),
    ("copilot-cowork-skills-dir", "copilot_cowork", True),
    ("registry.<name>.url", "registries", True),
    ("registry.<name>.token", "registries", True),
    ("registry.<name>.default", "registries", True),
)


def _valid_config_keys(*, unset: bool = False) -> str:
    """Return the keys the current flags expose, for messages."""
    from ..core.experimental import is_enabled

    return ", ".join(
        name
        for name, flag, unsettable in _CONFIG_KEYS
        if (flag is None or is_enabled(flag)) and (unsettable or not unset)
    )


def _reject_unknown_key(logger: CommandLogger, key: str, *, unset: bool = False) -> None:
    """Report an unknown key with the valid-key list and exit 1."""
    logger.error(f"Unknown configuration key: '{key}'")
    logger.progress(f"Valid keys: {_valid_config_keys(unset=unset)}")
    sys.exit(1)


def _echo_setting(key: str, value: object, *, indent: str = "") -> None:
    """Print one ``key: value`` line; the value renders through the owner."""
    click.echo(f"{indent}{key}: {render_config_value(key, value)}")


def _add_table_row(table, category: str, setting: str, value: object) -> None:
    """Add one ``apm config`` table row; the value renders through the owner."""
    table.add_row(category, setting, render_config_value(setting, value))


def _show_all_user_config(logger: CommandLogger) -> None:
    """Print all user-settable configuration keys with effective values."""
    from ..config import (
        get_allow_protocol_fallback,
        get_github_auth_first,
        get_install_target,
        get_mcp_registry_url,
        get_prefer_ssh,
        get_self_update_channel,
        get_self_update_install_dir,
        get_temp_dir,
    )

    logger.progress("APM Configuration:")
    temp_dir = get_temp_dir()
    _echo_setting(
        "temp-dir",
        temp_dir if temp_dir is not None else "Not set (using system default)",
        indent="  ",
    )
    install_target = get_install_target()
    if install_target is not None:
        _echo_setting("target", _render_target_value(install_target), indent="  ")
    self_update_channel = get_self_update_channel()
    if self_update_channel != "stable":
        _echo_setting("self-update.channel", self_update_channel, indent="  ")
    self_update_install_dir = get_self_update_install_dir()
    if self_update_install_dir is not None:
        _echo_setting("self-update.install-dir", self_update_install_dir, indent="  ")
    # Only show transport keys when non-default to reduce noise.
    if get_allow_protocol_fallback():
        click.echo("  allow-protocol-fallback: true")
    if get_prefer_ssh():
        click.echo("  prefer-ssh: true")
    if get_github_auth_first():
        click.echo("  github-auth-first: true")

    from ..core.experimental import is_enabled

    if is_enabled("external_scanners"):
        from ..config import get_audit_on_install, get_scanner_options
        from ..security.external.registry import SUPPORTED_SCANNERS

        _echo_setting("audit-on-install", get_audit_on_install(), indent="  ")
        for scanner in SUPPORTED_SCANNERS:
            llm, args = get_scanner_options(scanner)
            if llm is not None:
                _echo_setting(f"external.{scanner}.llm", str(llm).lower(), indent="  ")
            if args is not None:
                _echo_setting(f"external.{scanner}.args", " ".join(args), indent="  ")

    if is_enabled("copilot_cowork"):
        from ..config import get_copilot_cowork_skills_dir

        csd = get_copilot_cowork_skills_dir()
        _echo_setting(
            "copilot-cowork-skills-dir",
            csd if csd is not None else "Not set (using auto-detection)",
            indent="  ",
        )

    mcp_url = get_mcp_registry_url()
    _echo_setting(
        "mcp-registry-url",
        mcp_url if mcp_url is not None else "Not set (using default https://api.mcp.github.com)",
        indent="  ",
    )

    if is_enabled("registries"):
        from ..config import list_registry_configs

        for reg_name, body in sorted(list_registry_configs().items()):
            if "url" in body:
                _echo_setting(f"registry.{reg_name}.url", body["url"], indent="  ")
            if "token" in body:
                _echo_setting(f"registry.{reg_name}.token", body["token"], indent="  ")
            if body.get("default") is True:
                _echo_setting(f"registry.{reg_name}.default", True, indent="  ")


def _set_registry_key(logger: CommandLogger, reg_name: str, field: str, value: str) -> None:
    """Apply ``apm config set registry.<name>.<field> <value>``."""
    from ..config import set_registry_default, set_registry_token, set_registry_url

    if field == "url":
        try:
            set_registry_url(reg_name, value)
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        logger.success(f"registry.{reg_name}.url set")
    elif field == "token":
        set_registry_token(reg_name, value)
        logger.success(f"registry.{reg_name}.token set")
    else:
        try:
            is_default = _parse_bool_value(value)
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        set_registry_default(reg_name, is_default)
        if is_default:
            logger.success(f"registry.{reg_name}.default set")
        else:
            logger.success(f"registry.{reg_name}.default cleared")


def _require_external_scanners(logger, key: str) -> None:
    """Exit with an actionable error if the external-scanners flag is off."""
    from ..core.experimental import is_enabled

    if not is_enabled("external_scanners"):
        logger.error(
            f"'{key}' requires the external-scanners experimental flag. "
            f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='external-scanners').sentence()}"
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
            from apm_cli.utils.console import new_table

            console = _get_console()
            # Create configuration display
            config_table = new_table(
                title="Current APM Configuration",
                show_header=True,
                header_style="bold cyan",
            )
            config_table.add_column("Category", style="bold yellow", min_width=12)
            config_table.add_column("Setting", style="white", min_width=15)
            config_table.add_column("Value", style="cyan")

            # Show apm.yml if in project
            if manifest_file(Path(".")).exists():
                apm_config = _load_apm_config()
                _add_table_row(config_table, "Project", "Name", apm_config.get("name", "Unknown"))
                _add_table_row(config_table, "", "Version", apm_config.get("version", "Unknown"))
                _add_table_row(config_table, "", "Entrypoint", apm_config.get("entrypoint", "None"))
                _add_table_row(
                    config_table,
                    "",
                    "MCP Dependencies",
                    str(len(apm_config.get("dependencies", {}).get("mcp", []))),
                )

                # Show compilation configuration
                compilation_config = apm_config.get("compilation", {})
                if compilation_config:
                    _add_table_row(
                        config_table,
                        "Compilation",
                        "Output",
                        compilation_config.get("output", "AGENTS.md"),
                    )
                    _add_table_row(
                        config_table,
                        "",
                        "Chatmode",
                        compilation_config.get("chatmode", "auto-detect"),
                    )
                    _add_table_row(
                        config_table,
                        "",
                        "Resolve Links",
                        str(compilation_config.get("resolve_links", True)),
                    )
                else:
                    _add_table_row(
                        config_table, "Compilation", "Status", "Using defaults (no config)"
                    )
            else:
                _add_table_row(config_table, "Project", "Status", "Not in an APM project directory")

            _add_table_row(config_table, "Global", "APM CLI Version", get_version())

            from ..config import get_allow_protocol_fallback as _get_apf
            from ..config import get_github_auth_first as _get_gaf
            from ..config import get_prefer_ssh as _get_prefer_ssh_cfg
            from ..config import get_self_update_channel as _get_self_update_channel_cfg
            from ..config import get_self_update_install_dir as _get_self_update_install_dir_cfg
            from ..config import get_temp_dir as _get_temp_dir

            _temp_dir_val = _get_temp_dir()
            if _temp_dir_val:
                _add_table_row(config_table, "", "Temp Directory", _temp_dir_val)
            _self_update_channel_val = _get_self_update_channel_cfg()
            if _self_update_channel_val != "stable":
                _add_table_row(config_table, "", "Self-update Channel", _self_update_channel_val)
            _self_update_install_dir_val = _get_self_update_install_dir_cfg()
            if _self_update_install_dir_val:
                _add_table_row(
                    config_table,
                    "",
                    "Self-update Install Dir",
                    _self_update_install_dir_val,
                )

            # Only surface transport keys when they have been enabled -- the
            # false-default rows add noise for users who never configured them.
            _apf = _get_apf()
            _prefer_ssh = _get_prefer_ssh_cfg()
            _gaf = _get_gaf()
            if _apf:
                _add_table_row(config_table, "", "Allow Protocol Fallback", "true")
            if _prefer_ssh:
                _add_table_row(config_table, "", "Prefer SSH Transport", "true")
            if _gaf:
                _add_table_row(config_table, "", "GitHub Auth-first", "true")

            from ..core.experimental import is_enabled as _is_enabled

            if _is_enabled("copilot_cowork"):
                from ..config import get_copilot_cowork_skills_dir as _get_csd

                _csd_val = _get_csd()
                _add_table_row(
                    config_table,
                    "",
                    "Cowork Skills Dir",
                    _csd_val if _csd_val else "Not set (using auto-detection)",
                )

            console.print(config_table)

        except (ImportError, NameError):
            # Fallback display
            logger.progress("Current APM Configuration:")

            if manifest_file(Path(".")).exists():
                apm_config = _load_apm_config()
                click.echo(f"\n{HIGHLIGHT}Project (apm.yml):{RESET}")
                _echo_setting("Name", apm_config.get("name", "Unknown"), indent="  ")
                _echo_setting("Version", apm_config.get("version", "Unknown"), indent="  ")
                _echo_setting("Entrypoint", apm_config.get("entrypoint", "None"), indent="  ")
                _echo_setting(
                    "MCP Dependencies",
                    len(apm_config.get("dependencies", {}).get("mcp", [])),
                    indent="  ",
                )
            else:
                logger.progress("Not in an APM project directory")

            click.echo(f"\n{HIGHLIGHT}Global:{RESET}")
            _echo_setting("APM CLI Version", get_version(), indent="  ")

            from ..config import get_allow_protocol_fallback as _get_apf_fb
            from ..config import get_github_auth_first as _get_gaf_fb
            from ..config import get_prefer_ssh as _get_prefer_ssh_fb
            from ..config import get_self_update_channel as _get_self_update_channel_fb
            from ..config import get_self_update_install_dir as _get_self_update_install_dir_fb
            from ..config import get_temp_dir as _get_temp_dir_fb

            _temp_dir_fb = _get_temp_dir_fb()
            if _temp_dir_fb:
                _echo_setting("Temp Directory", _temp_dir_fb, indent="  ")
            _self_update_channel_fb = _get_self_update_channel_fb()
            if _self_update_channel_fb != "stable":
                _echo_setting("self-update.channel", _self_update_channel_fb, indent="  ")
            _self_update_install_dir_fb = _get_self_update_install_dir_fb()
            if _self_update_install_dir_fb:
                _echo_setting("self-update.install-dir", _self_update_install_dir_fb, indent="  ")

            _echo_setting("allow-protocol-fallback", str(_get_apf_fb()).lower(), indent="  ")
            _echo_setting("prefer-ssh", str(_get_prefer_ssh_fb()).lower(), indent="  ")
            _echo_setting("github-auth-first", str(_get_gaf_fb()).lower(), indent="  ")

            from ..core.experimental import is_enabled as _is_enabled_fb

            if _is_enabled_fb("copilot_cowork"):
                from ..config import get_copilot_cowork_skills_dir as _get_csd_fb

                _csd_fb = _get_csd_fb()
                _echo_setting(
                    "Cowork Skills Dir",
                    _csd_fb if _csd_fb else "Not set (using auto-detection)",
                    indent="  ",
                )


@config.command(help="Set a configuration value")
@click.argument("key")
@click.argument("value")
@serialized_lifecycle
def set(key, value):  # noqa: F811
    """Set a configuration value.

    Examples:
        apm config set target copilot
        apm config set prefer-ssh true
    """
    from ..config import get_temp_dir, set_temp_dir

    logger = CommandLogger("config set")

    registry_match = _REGISTRY_KEY_RE.match(key)
    if registry_match:
        from ..core.experimental import is_enabled

        if not is_enabled("registries"):
            logger.error(
                f"'{key}' requires the registries experimental flag. "
                f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='registries').sentence()}"
            )
            sys.exit(1)
        _set_registry_key(logger, registry_match.group(1), registry_match.group(2), value)
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
                logger.error(redact_secrets(str(exc)))
                sys.exit(1)
            set_scanner_llm(scanner_name, llm)
            logger.success(f"external.{scanner_name}.llm set to {'true' if llm else 'false'}")
        else:
            import shlex

            try:
                tokens = shlex.split(value, posix=(os.name != "nt"))
            except ValueError as exc:
                logger.error(f"Could not parse args value: {redact_secrets(str(exc))}")
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
                f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='copilot-cowork').sentence()}"
            )
            sys.exit(1)
        from ..config import get_copilot_cowork_skills_dir, set_copilot_cowork_skills_dir

        try:
            set_copilot_cowork_skills_dir(value)
            logger.success(
                "Cowork skills directory set to: "
                f"{render_config_value(key, get_copilot_cowork_skills_dir())}"
            )
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    if key == "temp-dir":
        try:
            set_temp_dir(value)
            logger.success(
                f"Temporary directory set to: {render_config_value(key, get_temp_dir())}"
            )
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    if key == "target":
        from ..config import set_install_target

        try:
            parsed = set_install_target(value)
            rendered = _render_target_value(parsed)
            logger.success(f"Default install target set to: {render_config_value(key, rendered)}")
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    if key == "self-update.channel":
        from ..config import set_self_update_channel

        try:
            channel = set_self_update_channel(value)
            logger.success(f"Self-update channel saved: {render_config_value(key, channel)}")
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    if key == "self-update.install-dir":
        from ..config import set_self_update_install_dir

        try:
            install_dir = set_self_update_install_dir(value)
            if self_update_command._is_windows_platform():
                logger.success(
                    f"Self-update install directory saved: {render_config_value(key, install_dir)}"
                )
            else:
                logger.success(
                    f"Self-update launcher directory preference saved: {render_config_value(key, install_dir)} "
                    "(must match the existing Unix installation)"
                )
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
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
            configured_url = get_mcp_registry_url()
            logger.success(f"MCP registry URL set to: {render_config_value(key, configured_url)}")
            if configured_url and configured_url.startswith("http://"):
                logger.warning(
                    "This endpoint uses plaintext HTTP; registry traffic is not encrypted. "
                    "Use only a trusted network or switch to https://."
                )
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    if key == "audit-on-install":
        from ..core.experimental import is_enabled

        if not is_enabled("external_scanners"):
            logger.error(
                "audit-on-install requires the external-scanners experimental flag. "
                f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='external-scanners').sentence()}"
            )
            sys.exit(1)
        from ..config import get_audit_on_install, set_audit_on_install

        try:
            set_audit_on_install(value)
            logger.success(
                f"Install-time audit set to: {render_config_value(key, get_audit_on_install())}"
            )
        except ValueError as exc:
            logger.error(redact_secrets(str(exc)))
            sys.exit(1)
        return

    setters = _get_config_setters()
    config_entry = setters.get(key)
    if config_entry is None:
        _reject_unknown_key(logger, key)

    try:
        enabled = _parse_bool_value(value)
    except ValueError as exc:
        logger.error(redact_secrets(str(exc)))
        sys.exit(1)

    setter, label = config_entry
    setter(enabled)
    if key in _DEPRECATED_CONFIG_KEYS:
        # A success line would imply the setting does something.
        logger.warning(_DEPRECATED_CONFIG_KEYS[key])
        logger.info(f"Stored {key}={'true' if enabled else 'false'} (no effect)")
    else:
        logger.success(f"{label} set to {'true' if enabled else 'false'}")

    # Warn when persisting allow-protocol-fallback=true in a CI environment where
    # $HOME is often shared across jobs -- the persisted value will affect all
    # subsequent apm install runs on that host. The env var is safer for CI.
    import os as _os

    if key == "allow-protocol-fallback" and enabled and _os.environ.get("CI"):
        from .. import config as config_module

        logger.warning(
            f"allow-protocol-fallback is now persisted to {config_module.CONFIG_FILE}. "
            "In CI environments with a shared $HOME this will affect all subsequent "
            "apm install runs on this host. "
            "Prefer APM_ALLOW_PROTOCOL_FALLBACK=1 as an invocation-scoped alternative."
        )


@config.command(help="Get a configuration value")
@click.argument("key", required=False)
def get(key):
    """Get a configuration value or show all configuration.

    Examples:
        apm config get target
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
                    f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='registries').sentence()}"
                )
                sys.exit(1)
            reg_name, field = registry_match.group(1), registry_match.group(2)
            from ..config import get_registry_config, is_registry_default

            if field == "default":
                _echo_setting(key, is_registry_default(reg_name))
                return
            _echo_setting(key, (get_registry_config(reg_name) or {}).get(field))
            return

        if key == "copilot-cowork-skills-dir":
            from ..config import get_copilot_cowork_skills_dir

            value = get_copilot_cowork_skills_dir()
            if value is None:
                click.echo("copilot-cowork-skills-dir: Not set (using auto-detection)")
            else:
                _echo_setting("copilot-cowork-skills-dir", value)
            return

        if key == "temp-dir":
            value = get_temp_dir()
            if value is None:
                click.echo("temp-dir: Not set (using system default)")
            else:
                _echo_setting("temp-dir", value)
            return

        if key == "target":
            from ..config import get_install_target

            value = get_install_target()
            if value is None:
                click.echo("target: Not set (using auto-detection)")
            else:
                _echo_setting("target", _render_target_value(value))
            return

        if key == "self-update.channel":
            from ..config import get_self_update_channel

            _echo_setting("self-update.channel", get_self_update_channel())
            return

        if key == "self-update.install-dir":
            from ..config import get_self_update_install_dir

            value = get_self_update_install_dir()
            if value is None:
                if self_update_command._is_windows_platform():
                    click.echo("self-update.install-dir: Not set (using installer default)")
                else:
                    click.echo(
                        "self-update.install-dir: Not set "
                        "(preserving the detected Unix installation)"
                    )
            else:
                _echo_setting("self-update.install-dir", value)
            return

        if key == "mcp-registry-url":
            from ..config import get_mcp_registry_url

            value = get_mcp_registry_url()
            if value is None:
                click.echo("mcp-registry-url: Not set (using default https://api.mcp.github.com)")
            else:
                _echo_setting("mcp-registry-url", value)
            return

        if key == "audit-on-install":
            from ..config import get_audit_on_install

            _echo_setting("audit-on-install", get_audit_on_install())
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
                    _echo_setting(key, str(llm).lower())
            elif args is None:
                click.echo(f"{key}: Not set")
            else:
                _echo_setting(key, " ".join(args))
            return

        getter = getters.get(key)
        if getter is None:
            _reject_unknown_key(logger, key)
        value = getter()
        # Render booleans as lowercase true/false (npm convention).
        if isinstance(value, bool):
            _echo_setting(key, str(value).lower())
        else:
            _echo_setting(key, value)
        if key in _DEPRECATED_CONFIG_KEYS:
            logger.warning(_DEPRECATED_CONFIG_KEYS[key])
    else:
        # Show all user-settable keys with their effective values (including
        # defaults).  Iterating raw config keys would hide settings that
        # have not been written yet (e.g. temp-dir on a fresh install).
        _show_all_user_config(logger)


@config.command(name="list", help="List all configuration values")
def list_config():
    """List all user-settable configuration values with effective values."""
    _show_all_user_config(CommandLogger("config list"))


@config.command(help="Unset a configuration value")
@click.argument("key")
@serialized_lifecycle
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
                f"{EXPERIMENTAL_FLAG_DISABLED.bind(flag='registries').sentence()}"
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
        if self_update_command._is_windows_platform():
            logger.success("Self-update install directory removed (will use installer default)")
        else:
            logger.success(
                "Self-update launcher directory preference removed "
                "(will preserve the detected Unix installation)"
            )
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

    if key == "github-auth-first":
        from ..config import unset_github_auth_first

        unset_github_auth_first()
        logger.success("GitHub auth-first preference removed (will use env var or default)")
        return

    if key == "copilot-cowork-skills-dir":
        from ..config import unset_copilot_cowork_skills_dir

        unset_copilot_cowork_skills_dir()
        logger.success("Cowork skills directory configuration removed")
        return

    _reject_unknown_key(logger, key, unset=True)
