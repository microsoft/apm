"""APM run and preview commands."""

import sys
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..utils.console import _rich_panel
from ._helpers import (
    HIGHLIGHT,
    RESET,
    _get_console,
    _get_default_script,
    _list_available_scripts,
    _rich_blank_line,
)


@click.command(help="Run a script with parameters")
@click.argument("script_name", required=False)
@click.option("--param", "-p", multiple=True, help="Parameter in format name=value")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def run(ctx, script_name, param, verbose):
    """Run a script from apm.yml (uses 'start' script if no name specified)."""
    logger = CommandLogger("run", verbose=verbose)
    try:
        # If no script name specified, use 'start' script
        if not script_name:
            script_name = _get_default_script()
            if not script_name:
                logger.error("No script specified and no 'start' script defined in apm.yml")
                logger.progress("Available scripts:")
                scripts = _list_available_scripts()

                console = _get_console()
                if console:
                    try:
                        from rich.table import Table

                        # Show available scripts in a table
                        table = Table(show_header=False, box=None, padding=(0, 1))
                        table.add_column("Icon", style="cyan")
                        table.add_column("Script", style="highlight")
                        table.add_column("Command", style="white")

                        for name, command in scripts.items():
                            table.add_row("  ", name, command)

                        console.print(table)
                    except Exception:
                        for name, command in scripts.items():
                            click.echo(f"  - {HIGHLIGHT}{name}{RESET}: {command}")
                else:
                    for name, command in scripts.items():
                        click.echo(f"  - {HIGHLIGHT}{name}{RESET}: {command}")
                sys.exit(1)

        # Parse parameters
        params = {}
        for p in param:
            if "=" in p:
                param_name, value = p.split("=", 1)
                params[param_name] = value
                logger.verbose_detail(f"  - {param_name}: {value}")

        # Import and use script runner
        try:
            from ..core.script_runner import ScriptRunner

            script_runner = ScriptRunner()
            success = script_runner.run_script(script_name, params)

            if not success:
                logger.error("Script execution failed")
                sys.exit(1)

            _rich_blank_line()
            logger.success("Script executed successfully!")

        except ImportError as ie:
            logger.warning("Script runner not available yet")
            logger.verbose_detail(f"Import error: {ie}")
            logger.verbose_detail(f"Would run script: {script_name} with params {params}")
        except Exception as ee:
            logger.error(f"Script execution error: {ee}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error running script: {e}")
        sys.exit(1)


@click.command(help="Preview a script's compiled prompt files")
@click.argument("script_name", required=False)
@click.option("--param", "-p", multiple=True, help="Parameter in format name=value")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def preview(ctx, script_name, param, verbose):
    """Preview compiled prompt files for a script."""
    logger = CommandLogger("preview", verbose=verbose)
    try:
        # If no script name specified, use 'start' script
        if not script_name:
            script_name = _get_default_script()
            if not script_name:
                logger.error("No script specified and no 'start' script defined in apm.yml")
                sys.exit(1)

        logger.start(f"Previewing script: {script_name}")

        # Parse parameters
        params = {}
        for p in param:
            if "=" in p:
                param_name, value = p.split("=", 1)
                params[param_name] = value
                logger.verbose_detail(f"  - {param_name}: {value}")

        # Import and use script runner for preview
        try:
            from ..core.script_runner import ScriptRunner

            script_runner = ScriptRunner()

            # Get the script command
            scripts = script_runner.list_scripts()
            if script_name not in scripts:
                logger.error(f"Script '{script_name}' not found")
                sys.exit(1)

            command = scripts[script_name]

            try:
                # Show original and compiled commands in panels
                _rich_panel(command, title=" Original command", style="blue")

                # Auto-compile prompts to show what would be executed
                compiled_command, compiled_prompt_files = script_runner._auto_compile_prompts(
                    command, params
                )

                if compiled_prompt_files:
                    _rich_panel(compiled_command, title="> Compiled command", style="green")
                else:
                    _rich_panel(
                        compiled_command,
                        title="> Command (no prompt compilation)",
                        style="yellow",
                    )
                    logger.warning(
                        "No .prompt.md files found in command. APM only compiles files ending with '.prompt.md'"
                    )

                # Show compiled files if any .prompt.md files were processed
                if compiled_prompt_files:
                    file_list = []
                    for prompt_file in compiled_prompt_files:
                        output_name = Path(prompt_file).stem.replace(".prompt", "") + ".txt"
                        compiled_path = Path(".apm/compiled") / output_name
                        file_list.append(str(compiled_path))

                    files_content = "\n".join([f" {file}" for file in file_list])
                    _rich_panel(files_content, title=" Compiled prompt files", style="cyan")
                else:
                    _rich_panel(
                        "No .prompt.md files were compiled.\n\n"
                        + "APM only compiles files ending with '.prompt.md' extension.\n"
                        + "Other files are executed as-is by the runtime.",
                        title="[i]  Compilation Info",
                        style="cyan",
                    )

            except (ImportError, NameError):
                # Fallback display
                logger.progress("Original command:")
                click.echo(f"  {command}")

                compiled_command, compiled_prompt_files = script_runner._auto_compile_prompts(
                    command, params
                )

                if compiled_prompt_files:
                    logger.progress("Compiled command:")
                    click.echo(f"  {compiled_command}")

                    logger.progress("Compiled prompt files:")
                    for prompt_file in compiled_prompt_files:
                        output_name = Path(prompt_file).stem.replace(".prompt", "") + ".txt"
                        compiled_path = Path(".apm/compiled") / output_name
                        click.echo(f"  - {compiled_path}")
                else:
                    logger.warning("Command (no prompt compilation):")
                    click.echo(f"  {compiled_command}")
                    logger.progress("APM only compiles files ending with '.prompt.md' extension.")

            _rich_blank_line()
            logger.success(
                f"Preview complete! Use 'apm run {script_name}' to execute.",
            )

        except ImportError:
            logger.warning("Script runner not available yet")

    except Exception as e:
        logger.error(f"Error previewing script: {e}")
        sys.exit(1)
