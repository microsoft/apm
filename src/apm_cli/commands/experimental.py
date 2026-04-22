"""APM experimental feature-flag command group.

Provides ``apm experimental list|enable|disable|reset`` to manage
opt-in feature flags stored in ``~/.apm/config.json``.
"""

import json
import sys

import click

from ._helpers import _get_console
from ..core.command_logger import CommandLogger
from ..core.experimental import (
    FLAGS,
    display_name,
    normalise_flag_name,
    validate_flag_name,
    enable as _enable_flag,
    disable as _disable_flag,
    get_malformed_flag_keys,
    get_overridden_flags,
    get_stale_config_keys,
    reset as _reset_flags,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _resolve_verbose(ctx: click.Context, local_verbose: bool) -> bool:
    """Merge subcommand-local ``--verbose`` with the group-level value.

    Prefers the subcommand-local flag when explicitly passed; otherwise
    inherits from the group's ``ctx.obj["verbose"]``.
    """
    if local_verbose:
        return True
    return (ctx.obj.get("verbose", False) if ctx.obj else False)


def _print_list_footer(flags_shown: list, logger: "CommandLogger") -> None:
    """Print the footer hint and stale-key note after the flag listing.

    Shared by both the Rich table and plain-text rendering paths.
    """
    from ..core.experimental import is_enabled

    enabled_count = sum(1 for f in flags_shown if is_enabled(f.name))
    if enabled_count == 0:
        logger.progress("Tip: apm experimental enable <name>")
    else:
        logger.progress("Tip: apm experimental disable <name> to revert")

    stale = get_stale_config_keys()
    if stale:
        logger.progress(
            f"Note: {len(stale)} unknown flag(s) in config "
            "(run 'apm experimental reset' to clean up)"
        )


def _build_table(flags_to_show, logger):
    """Build and print a Rich table of experimental flags.

    Falls back to plain-text output when Rich is unavailable.
    """
    from ..core.experimental import is_enabled

    try:
        from rich.table import Table

        console = _get_console()
        if console is None:
            raise ImportError("Rich console unavailable")

        table = Table(
            title="Experimental Features",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Flag", style="bold white", min_width=18)
        table.add_column("Status", min_width=10)
        table.add_column("Description", style="white", min_width=30)

        for flag in flags_to_show:
            enabled = is_enabled(flag.name)
            status_word = "enabled" if enabled else "disabled"
            status_cell = f"[{'green bold' if enabled else 'dim'}]{status_word}[/]"
            table.add_row(
                display_name(flag.name),
                status_cell,
                flag.description,
            )

        console.print(table)
        _print_list_footer(flags_to_show, logger)

    except ImportError:
        # Rich not installed -- plain-text fallback
        from ..utils.console import _rich_echo

        for flag in flags_to_show:
            enabled = is_enabled(flag.name)
            status = "enabled" if enabled else "disabled"
            _rich_echo(f"  {display_name(flag.name)} [{status}]", color="white", bold=True)
            _rich_echo(f"    {flag.description}", color="dim")

        _print_list_footer(flags_to_show, logger)


def _handle_unknown_flag(name: str, logger: CommandLogger) -> None:
    """Handle an unknown flag name: print error, suggestions, and exit.

    Callers are responsible for passing a normalised (snake_case) name.
    """
    try:
        validate_flag_name(name)
    except ValueError as exc:
        args = exc.args
        display = display_name(name)
        logger.error(f"Unknown experimental feature: {display}")

        suggestions = args[1] if len(args) > 1 else []
        if len(suggestions) == 1:
            logger.progress(f"Did you mean: {suggestions[0]}?")
        elif len(suggestions) > 1:
            logger.progress(f"Similar features: {', '.join(suggestions)}")

        logger.progress("Run 'apm experimental list' to see all available features.")
        sys.exit(1)


# ------------------------------------------------------------------
# Click command group
# ------------------------------------------------------------------

@click.group(
    help="Manage experimental feature flags",
    invoke_without_command=True,
    context_settings={"allow_interspersed_args": True, "ignore_unknown_options": True},
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show verbose output")
@click.pass_context
def experimental(ctx, verbose: bool):
    """Manage experimental feature flags."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if ctx.invoked_subcommand is None:
        # Default subcommand: list
        ctx.invoke(list_flags)


@experimental.command("list", help="List all experimental features")
@click.option("--enabled", "filter_enabled", is_flag=True, default=False, help="Show only enabled features")
@click.option("--disabled", "filter_disabled", is_flag=True, default=False, help="Show only disabled features")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show detailed output")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON array")
@click.pass_context
def list_flags(ctx, filter_enabled: bool, filter_disabled: bool, verbose: bool, as_json: bool):
    """List all registered experimental flags."""
    if filter_enabled and filter_disabled:
        raise click.UsageError("--enabled and --disabled are mutually exclusive.")

    verbose = _resolve_verbose(ctx, verbose)
    logger = CommandLogger("experimental list", verbose=verbose)

    from ..config import CONFIG_FILE

    logger.verbose_detail(f"Config file: {CONFIG_FILE}")

    all_flags = list(FLAGS.values())

    if filter_enabled:
        from ..core.experimental import is_enabled

        flags_to_show = [f for f in all_flags if is_enabled(f.name)]
        if not flags_to_show and not as_json:
            logger.progress("No experimental flags are enabled.")
            return
    elif filter_disabled:
        from ..core.experimental import is_enabled

        flags_to_show = [f for f in all_flags if not is_enabled(f.name)]
        if not flags_to_show and not as_json:
            logger.progress("All experimental flags are currently enabled.")
            return
    else:
        flags_to_show = all_flags

    if as_json:
        from ..core.experimental import is_enabled

        overridden = get_overridden_flags()
        rows = []
        for flag in flags_to_show:
            rows.append({
                "name": flag.name,
                "enabled": is_enabled(flag.name),
                "default": flag.default,
                "description": flag.description,
                "source": "config" if flag.name in overridden else "default",
            })
        click.echo(json.dumps(rows, indent=2))
        return

    logger.verbose_detail("Experimental features let you try new behaviour before it becomes default.")
    _build_table(flags_to_show, logger)


@experimental.command("enable", help="Enable an experimental feature")
@click.argument("name")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show detailed output")
@click.pass_context
def enable_flag(ctx, name: str, verbose: bool):
    """Enable an experimental feature flag."""
    verbose = _resolve_verbose(ctx, verbose)
    logger = CommandLogger("experimental enable", verbose=verbose)

    from ..config import CONFIG_FILE

    logger.verbose_detail(f"Config file: {CONFIG_FILE}")

    normalised = normalise_flag_name(name)
    if normalised not in FLAGS:
        _handle_unknown_flag(normalised, logger)
        return  # unreachable after sys.exit, but satisfies linters

    from ..core.experimental import is_enabled

    if is_enabled(normalised):
        logger.warning(f"{display_name(normalised)} is already enabled.")
        return

    flag = _enable_flag(normalised)
    logger.success(f"Enabled experimental feature: {display_name(normalised)}", symbol="check")
    if flag.hint:
        logger.progress(flag.hint)


@experimental.command("disable", help="Disable an experimental feature")
@click.argument("name")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show detailed output")
@click.pass_context
def disable_flag(ctx, name: str, verbose: bool):
    """Disable an experimental feature flag."""
    verbose = _resolve_verbose(ctx, verbose)
    logger = CommandLogger("experimental disable", verbose=verbose)

    from ..config import CONFIG_FILE

    logger.verbose_detail(f"Config file: {CONFIG_FILE}")

    normalised = normalise_flag_name(name)
    if normalised not in FLAGS:
        _handle_unknown_flag(normalised, logger)
        return

    from ..core.experimental import is_enabled

    if not is_enabled(normalised):
        logger.warning(f"{display_name(normalised)} is already disabled.")
        return

    _disable_flag(normalised)
    logger.success(f"Disabled experimental feature: {display_name(normalised)}", symbol="check")


@experimental.command("reset", help="Reset experimental features to defaults")
@click.argument("name", required=False, default=None)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show detailed output")
@click.pass_context
def reset_flags(ctx, name: str | None, yes: bool, verbose: bool):
    """Reset one or all experimental features to their defaults."""
    verbose = _resolve_verbose(ctx, verbose)
    logger = CommandLogger("experimental reset", verbose=verbose)

    from ..config import CONFIG_FILE

    logger.verbose_detail(f"Config file: {CONFIG_FILE}")

    if name is not None:
        # Single-flag reset
        normalised = normalise_flag_name(name)
        if normalised not in FLAGS:
            _handle_unknown_flag(normalised, logger)
            return

        reset_result = _reset_flags(normalised)
        default_label = "enabled" if FLAGS[normalised].default else "disabled"
        if reset_result > 0:
            logger.success(
                f"Reset {display_name(normalised)} to default ({default_label})",
                symbol="check",
            )
        else:
            logger.progress(
                f"{display_name(normalised)} is already at its default ({default_label}). Nothing to do."
            )
        return

    # Bulk reset -- collect overrides (known bool, stale, and malformed)
    overridden = get_overridden_flags()
    stale = get_stale_config_keys()
    malformed = get_malformed_flag_keys()

    if not overridden and not stale and not malformed:
        logger.progress("All features already at default settings. Nothing to reset.")
        return

    # Build confirmation listing
    if not yes:
        lines = []
        for flag_name, value in overridden.items():
            current = "enabled" if value else "disabled"
            default = "enabled" if FLAGS[flag_name].default else "disabled"
            if current == default:
                lines.append(f"  {display_name(flag_name)} (redundant override - removing)")
            else:
                lines.append(f"  {display_name(flag_name)} (currently {current} -> {default})")
        for key in stale:
            lines.append(f"  {display_name(key)} (unknown, will be removed)")
        for key in malformed:
            lines.append(f"  {display_name(key)} (malformed value, will be removed)")

        total = len(overridden) + len(stale) + len(malformed)
        noun = "feature" if total == 1 else "features"
        pronoun = "its" if total == 1 else "their"
        default_word = "default" if total == 1 else "defaults"
        logger.progress(f"This will reset {total} experimental {noun} to {pronoun} {default_word}:")
        for line in lines:
            logger.progress(line)

        try:
            from rich.prompt import Confirm

            confirmed = Confirm.ask("Proceed?", default=False)
        except ImportError:
            confirmed = click.confirm("Proceed?", default=False)

        if not confirmed:
            logger.progress("Operation cancelled")
            return

    _reset_flags(None)
    logger.success("Reset all experimental features to defaults", symbol="check")
