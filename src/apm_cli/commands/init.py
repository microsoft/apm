"""APM init command."""

import os
import sys
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..utils.console import (
    _create_files_table,
    _rich_panel,
)
from ._helpers import (
    INFO,
    RESET,
    _create_minimal_apm_yml,
    _create_plugin_json,
    _get_console,
    _get_default_config,
    _rich_blank_line,
    _validate_plugin_name,
    _validate_project_name,
)


@click.command(help="Initialize a new APM project")
@click.argument("project_name", required=False)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts and use auto-detected defaults"
)
@click.option(
    "--plugin", is_flag=True, help="Initialize as plugin author (creates plugin.json + apm.yml)"
)
@click.option(
    "--marketplace",
    "marketplace_flag",
    is_flag=True,
    help="Seed apm.yml with a 'marketplace:' authoring block",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def init(ctx, project_name, yes, plugin, marketplace_flag, verbose):
    """Initialize a new APM project (like npm init).

    Creates a minimal apm.yml with auto-detected metadata.
    With --plugin, also creates plugin.json for plugin authors.
    With --marketplace, also seeds apm.yml with a marketplace authoring block.
    """
    logger = CommandLogger("init", verbose=verbose)
    try:
        # Handle explicit current directory
        if project_name == ".":
            project_name = None

        # Reject names containing path separators before any filesystem use
        if project_name and not _validate_project_name(project_name):
            logger.error(
                f"Invalid project name '{project_name}': "
                "project names must not contain path separators ('/' or '\\\\') or be '..'."
            )
            sys.exit(1)

        # Determine project directory and name
        if project_name:
            project_dir = Path(project_name)
            project_dir.mkdir(exist_ok=True)
            os.chdir(project_dir)
            logger.progress(f"Created project directory: {project_name}", symbol="folder")
            final_project_name = project_name
        else:
            project_dir = Path.cwd()
            final_project_name = project_dir.name

        # Validate plugin name early
        if plugin and not _validate_plugin_name(final_project_name):
            logger.error(
                f"Invalid plugin name '{final_project_name}'. "
                "Must be kebab-case (lowercase letters, numbers, hyphens), "
                "start with a letter, and be at most 64 characters."
            )
            sys.exit(1)

        # Check for existing apm.yml
        apm_yml_exists = Path(APM_YML_FILENAME).exists()

        # Handle existing apm.yml in brownfield projects
        if apm_yml_exists:
            logger.warning("apm.yml already exists")

            if not yes:
                confirm = click.confirm("Continue and overwrite?")

                if not confirm:
                    logger.progress("Initialization cancelled.")
                    return
            else:
                logger.progress("--yes specified, overwriting apm.yml...")

        # Get project configuration (interactive mode or defaults)
        if not yes:
            config = _interactive_project_setup(final_project_name, logger)
        else:
            # Use auto-detected defaults
            config = _get_default_config(final_project_name)

        # Plugin mode uses 0.1.0 as default version
        if plugin and yes:
            config["version"] = "0.1.0"

        logger.start(f"Initializing APM project: {config['name']}", symbol="running")

        # Create apm.yml (with devDependencies for plugin mode)
        _create_minimal_apm_yml(config, plugin=plugin)

        # Create plugin.json for plugin mode
        if plugin:
            _create_plugin_json(config)

        # Append marketplace authoring block when requested.
        if marketplace_flag:
            from ..marketplace.init_template import render_marketplace_block

            apm_yml_path = Path.cwd() / APM_YML_FILENAME
            try:
                existing = apm_yml_path.read_text(encoding="utf-8")
                if not existing.endswith("\n"):
                    existing += "\n"
                # Owner is intentionally left to the template default
                # (acme-org placeholder). Deriving it from the project
                # name produced misleading https://github.com/<project>
                # URLs; the user is expected to edit the placeholder.
                block = render_marketplace_block()
                apm_yml_path.write_text(existing + "\n" + block, encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    f"Failed to append marketplace block to apm.yml: {exc}",
                    symbol="warning",
                )

        logger.success("APM project initialized successfully!")

        # Display created file info
        try:
            console = _get_console()
            if console:
                files_data = [
                    ("*", APM_YML_FILENAME, "Project configuration"),
                ]
                if plugin:
                    files_data.append(("*", "plugin.json", "Plugin metadata"))
                table = _create_files_table(files_data, title="Created Files")
                console.print(table)
        except (ImportError, NameError):
            logger.progress("Created:")
            click.echo("  * apm.yml - Project configuration")
            if plugin:
                click.echo("  * plugin.json - Plugin metadata")

        _rich_blank_line()

        # Next steps - actionable commands matching README workflow
        if plugin:
            next_steps = [
                "Add dev dependencies:    apm install --dev <owner>/<repo>",
                "Pack as plugin:          apm pack",
            ]
        else:
            next_steps = [
                "Install a skill:                apm install github/awesome-copilot/skills/documentation-writer",
                "Install a marketplace plugin:   apm install frontend-web-dev@awesome-copilot",
                "Install a versioned package:    apm install microsoft/apm-sample-package#v1.0.0",
                "Author your own plugin:         apm pack",
            ]

        try:
            _rich_panel(
                "\n".join(f"* {step}" for step in next_steps),
                title=" Next Steps",
                style="cyan",
            )
        except (ImportError, NameError):
            logger.progress("Next steps:")
            for step in next_steps:
                click.echo(f"  * {step}")

        # Footer with links
        try:
            console = _get_console()
            if console:
                console.print(
                    "  Docs: https://microsoft.github.io/apm  |  "
                    "Star: https://github.com/microsoft/apm",
                    style="dim",
                )
            else:
                click.echo(
                    "  Docs: https://microsoft.github.io/apm  |  "
                    "Star: https://github.com/microsoft/apm"
                )
        except (ImportError, NameError):
            click.echo(
                "  Docs: https://microsoft.github.io/apm  |  Star: https://github.com/microsoft/apm"
            )

    except Exception as e:
        logger.error(f"Error initializing project: {e}")
        sys.exit(1)


def _interactive_project_setup(default_name, logger):
    """Interactive setup for new APM projects with auto-detection."""
    from ._helpers import _auto_detect_author, _auto_detect_description, _validate_project_name

    # Get auto-detected defaults
    auto_author = _auto_detect_author()
    auto_description = _auto_detect_description(default_name)

    try:
        # Lazy import rich pieces
        from rich.console import Console  # type: ignore
        from rich.panel import Panel  # type: ignore
        from rich.prompt import Confirm, Prompt  # type: ignore

        console = _get_console() or Console()
        console.print("\n[info]Setting up your APM project...[/info]")
        console.print("[muted]Press ^C at any time to quit.[/muted]\n")

        while True:
            name = Prompt.ask("Project name", default=default_name).strip()
            if _validate_project_name(name):
                break
            console.print(
                f"[error]Invalid project name '{name}': "
                "project names must not contain path separators ('/' or '\\\\') or be '..'.[/error]"
            )

        version = Prompt.ask("Version", default="1.0.0").strip()
        description = Prompt.ask("Description", default=auto_description).strip()
        author = Prompt.ask("Author", default=auto_author).strip()

        summary_content = f"""name: {name}
version: {version}
description: {description}
author: {author}"""
        console.print(Panel(summary_content, title="About to create", border_style="cyan"))

        if not Confirm.ask("\nIs this OK?", default=True):
            console.print("[info]Aborted.[/info]")
            sys.exit(0)

    except (ImportError, NameError):
        # Fallback to click prompts
        logger.progress("Setting up your APM project...")
        logger.progress("Press ^C at any time to quit.")

        while True:
            name = click.prompt("Project name", default=default_name).strip()
            if _validate_project_name(name):
                break
            click.echo(
                f"{ERROR}Invalid project name '{name}': "
                f"project names must not contain path separators ('/' or '\\\\') or be '..'.{RESET}"
            )

        version = click.prompt("Version", default="1.0.0").strip()
        description = click.prompt("Description", default=auto_description).strip()
        author = click.prompt("Author", default=auto_author).strip()

        click.echo(f"\n{INFO}About to create:{RESET}")
        click.echo(f"  name: {name}")
        click.echo(f"  version: {version}")
        click.echo(f"  description: {description}")
        click.echo(f"  author: {author}")

        if not click.confirm("\nIs this OK?", default=True):
            logger.progress("Aborted.")
            sys.exit(0)

    return {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
    }
