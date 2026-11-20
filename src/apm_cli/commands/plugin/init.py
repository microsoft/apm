"""``apm plugin init`` -- scaffold an Agent Plugin (plugin.json + apm.yml).

Thin wrapper that delegates to the shared ``_perform_init`` helper
in ``apm_cli.commands.init`` with ``plugin=True``. It replaces the removed
``apm init --plugin`` flag (#4073), including its metadata options.
"""

from __future__ import annotations

import click

from ...bundle.formats import (
    BundleFormat,
    cli_plugin_format_choices,
    resolve_bundle_format,
)
from ...core.target_detection import TargetParamType
from ...install.locking import serialized_lifecycle
from ..init import _perform_init, _refuse_ide_target_first


@click.command(help="Scaffold a plugin project (creates plugin.json + apm.yml)")
@click.argument("project_name", required=False)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts and use auto-detected defaults"
)
@click.option(
    "--target",
    "target_flag",
    type=TargetParamType(),
    default=None,
    help="Comma-separated target list (skip prompt, write directly)",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(cli_plugin_format_choices(), case_sensitive=False),
    default=None,
    help=(
        "Plugin format. 'agent-plugin' selects portable Agent Plugins v1; "
        "'plugin', 'claude', and 'claude-plugin' select the current "
        "Claude-compatible default."
    ),
)
@click.option(
    "--claude-plugin",
    "claude_plugin",
    is_flag=True,
    help="Scaffold the legacy Claude-compatible layout (current no-flag default)",
)
@click.option("--description", default=None, help="Plugin description (default: auto-detected)")
@click.option(
    "--version",
    default=None,
    help="Plugin version (default: 1.0.0, or 0.1.0 with --yes)",
)
@click.option("--author", default=None, help="Plugin author (default: git config user.name)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@_refuse_ide_target_first("plugin")
@serialized_lifecycle
def init(
    project_name,
    yes,
    target_flag,
    fmt,
    claude_plugin,
    verbose,
    description=None,
    version=None,
    author=None,
):
    """Initialize a plugin project (like ``cargo new --lib``).

    Replaces the removed ``apm init --plugin`` flag. Use
    ``apm marketplace init`` to publish a marketplace.
    """
    try:
        bundle_format = resolve_bundle_format(fmt, claude_plugin=claude_plugin)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    plugin_mode = "agent" if bundle_format is BundleFormat.AGENT_PLUGIN else "claude"

    _perform_init(
        project_name=project_name,
        yes=yes,
        plugin=True,
        target_flag=target_flag,
        description=description,
        version=version,
        author=author,
        verbose=verbose,
        source="plugin",
        plugin_mode=plugin_mode,
    )
