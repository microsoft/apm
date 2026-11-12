"""``apm plugin init`` -- scaffold an Agent Plugin (plugin.json + apm.yml).

Thin wrapper that delegates to the shared ``_perform_init`` helper
in ``apm_cli.commands.init`` with ``plugin=True``. This guarantees
byte-for-byte output parity with the deprecated ``apm init --plugin``
flag while letting users discover the plugin-author noun namespace.
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
from ..init import _perform_init


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
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@serialized_lifecycle
def init(project_name, yes, target_flag, fmt, claude_plugin, verbose):
    """Initialize a plugin project (like ``cargo new --lib``).

    Equivalent to the deprecated ``apm init --plugin`` flag. Use
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
        marketplace_flag=False,
        target_flag=target_flag,
        verbose=verbose,
        source="plugin",
        plugin_mode=plugin_mode,
    )
