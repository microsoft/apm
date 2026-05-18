"""APM init command."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..core.target_detection import TargetParamType
from . import init_helpers as _init_helpers
from ._helpers import (
    _create_minimal_apm_yml,
    _create_plugin_json,
    _get_default_config,
    _rich_blank_line,
    _validate_plugin_name,
    _validate_project_name,
)
from .init_helpers import (
    _append_marketplace_block,
    _confirm_setup_summary,
    _handle_existing_manifest,
    _interactive_project_setup,
    _normalise_project_name,
    _parse_toggle_input,
    _prepare_project_directory,
    _render_created_files,
    _render_footer,
    _render_next_steps,
    _resolve_init_targets,
    _stdin_is_tty,
)


def _validate_init_project(project_name, plugin: bool, logger):
    """Validate the requested project name and prepare the working directory."""
    project_name = _normalise_project_name(project_name)
    if project_name and not _validate_project_name(project_name):
        logger.error(
            f"Invalid project name '{project_name}': "
            "project names must not contain path separators ('/' or '\\\\') or be '..'."
        )
        sys.exit(1)

    _project_dir, final_project_name = _prepare_project_directory(project_name, logger)
    if plugin and not _validate_plugin_name(final_project_name):
        logger.error(
            f"Invalid plugin name '{final_project_name}'. "
            "Must be kebab-case (lowercase letters, numbers, hyphens), "
            "start with a letter, and be at most 64 characters."
        )
        sys.exit(1)
    return final_project_name


def _build_init_config(final_project_name, yes: bool, target_flag, apm_yml_exists: bool, logger):
    """Collect config interactively or from defaults, then resolve targets."""
    config = (
        _interactive_project_setup(final_project_name, logger)
        if not yes
        else _get_default_config(final_project_name)
    )
    resolved_targets = _resolve_init_targets(
        project_root=Path.cwd(),
        target_flag=target_flag,
        yes=yes,
        apm_yml_exists=apm_yml_exists,
        logger=logger,
    )
    if resolved_targets is not None:
        config["targets"] = sorted(resolved_targets)
    if not yes:
        _confirm_setup_summary(config, logger)
    return config


def _perform_init(
    *,
    project_name,
    yes,
    plugin,
    marketplace_flag,
    target_flag,
    verbose,
    source="init",
):
    """Shared init body called by ``apm init`` and ``apm plugin init``.

    ``source`` controls the CommandLogger prefix and "Next steps" hint shape.
    """
    logger = CommandLogger(source, verbose=verbose)
    _init_helpers._stdin_is_tty = _stdin_is_tty
    _init_helpers._parse_toggle_input = _parse_toggle_input
    try:
        final_project_name = _validate_init_project(project_name, plugin, logger)

        apm_yml_exists = Path(APM_YML_FILENAME).exists()
        if not _handle_existing_manifest(apm_yml_exists, yes, logger):
            logger.progress("Initialization cancelled.")
            return

        config = _build_init_config(
            final_project_name,
            yes,
            target_flag,
            apm_yml_exists,
            logger,
        )
        if plugin and yes:
            config["version"] = "0.1.0"

        logger.start(f"Initializing APM project: {config['name']}", symbol="running")
        _create_minimal_apm_yml(config, plugin=plugin)
        if plugin:
            _create_plugin_json(config)
        if marketplace_flag:
            _append_marketplace_block(logger)

        logger.success("APM project initialized successfully!")
        _render_created_files(plugin, logger)
        _rich_blank_line()
        _render_next_steps(plugin, logger)
        if Path(".codex").is_dir():
            logger.progress(
                "Tip: Use '--target agent-skills' to also deploy skills to "
                ".agents/skills/ for other clients.",
                symbol="info",
            )
        _render_footer()

    except Exception as e:
        logger.error(f"Error initializing project: {e}")
        sys.exit(1)


@click.command(help="Initialize a new APM project")
@click.argument("project_name", required=False)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts and use auto-detected defaults"
)
@click.option(
    "--plugin",
    is_flag=True,
    help="(deprecated) Use 'apm plugin init' instead. Scaffolds plugin.json + apm.yml.",
)
@click.option(
    "--marketplace",
    "marketplace_flag",
    is_flag=True,
    help="(deprecated) Use 'apm marketplace init' instead. Seeds a marketplace block.",
)
@click.option(
    "--target",
    "target_flag",
    type=TargetParamType(),
    default=None,
    help="Comma-separated target list (skip prompt, write directly)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def init(ctx, project_name, **options):
    """Initialize a new APM project (like npm init).

    Creates a minimal apm.yml with auto-detected metadata.

    Producers: prefer 'apm plugin init' (plugin scaffold) or
    'apm marketplace init' (marketplace block). The --plugin and
    --marketplace flags on 'apm init' are kept for backward
    compatibility and will be removed in v0.16.
    """
    yes = options["yes"]
    plugin = options["plugin"]
    marketplace_flag = options["marketplace_flag"]
    target_flag = options["target_flag"]
    verbose = options["verbose"]

    # Runtime deprecation warnings (flags still work but will be removed in v0.16).
    if plugin:
        click.echo(
            "[!] --plugin is deprecated and will be removed in v0.16; "
            "use 'apm plugin init' instead.",
            err=True,
        )
    if marketplace_flag:
        click.echo(
            "[!] --marketplace is deprecated and will be removed in v0.16; "
            "use 'apm marketplace init' instead.",
            err=True,
        )

    _init_helpers._stdin_is_tty = _stdin_is_tty
    _init_helpers._parse_toggle_input = _parse_toggle_input
    logger = CommandLogger("init", verbose=verbose)
    try:
        final_project_name = _validate_init_project(project_name, plugin, logger)

        apm_yml_exists = Path(APM_YML_FILENAME).exists()
        if not _handle_existing_manifest(apm_yml_exists, yes, logger):
            logger.progress("Initialization cancelled.")
            return

        config = _build_init_config(
            final_project_name,
            yes,
            target_flag,
            apm_yml_exists,
            logger,
        )
        if plugin and yes:
            config["version"] = "0.1.0"

        logger.start(f"Initializing APM project: {config['name']}", symbol="running")
        _create_minimal_apm_yml(config, plugin=plugin)
        if plugin:
            _create_plugin_json(config)
        if marketplace_flag:
            _append_marketplace_block(logger)

        logger.success("APM project initialized successfully!")
        _render_created_files(plugin, logger)
        _rich_blank_line()
        _render_next_steps(plugin, logger)
        if Path(".codex").is_dir():
            logger.progress(
                "Tip: Use '--target agent-skills' to also deploy skills to "
                ".agents/skills/ for other clients.",
                symbol="info",
            )
        _render_footer()

    except Exception as e:
        logger.error(f"Error initializing project: {e}")
        sys.exit(1)
