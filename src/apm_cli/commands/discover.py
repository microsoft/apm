"""Read-only native inventory and explicitly consented consumer declarations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click


def discover_options(command: Any) -> Any:
    """Share flags between init --discover and its hidden alias."""
    for option in reversed(
        (
            click.option(
                "--apply",
                "--write",
                "write",
                is_flag=True,
                help="Add existing local package references to consumer apm.yml; never install.",
            ),
            click.option(
                "--format",
                "output_format",
                type=click.Choice(["text", "json", "yaml"]),
                default="text",
                show_default=True,
                help="Discovery report format",
            ),
            click.option(
                "--global",
                "-g",
                "global_",
                is_flag=True,
                help="Discover under home and declare packages in the APM user manifest",
            ),
        )
    ):
        command = option(command)
    return command


def run_discover(
    *, write: bool, output_format: str, global_: bool, yes: bool, verbose: bool
) -> None:
    """Preview before consent; acquire no lifecycle lock on read-only paths."""
    import yaml

    from apm_cli.adopt.discovery import discover as inventory
    from apm_cli.adopt.materialize import apply_report
    from apm_cli.adopt.render import DiscoveryLogger
    from apm_cli.core.scope import InstallScope, get_manifest_path

    logger = DiscoveryLogger("discover", verbose=verbose)
    root = Path.home() if global_ else Path.cwd()
    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    try:
        report = inventory(root, get_manifest_path(scope), user_scope=global_)
        if write:
            if any(item["status"] == "unsafe" for item in report["findings"]):
                logger.report(report, output_format)
                raise click.ClickException(
                    "Unsafe paths or install identity collisions found. Repair the listed inputs and retry."
                )
            if not yes:
                if not sys.stdin.isatty() or output_format != "text":
                    raise click.ClickException(
                        "Applying discovery requires consent. Review --discover, then rerun --apply --yes."
                    )
                logger.report(report, "text")
                if not click.confirm(
                    "Add the listed dependencies to consumer apm.yml?", default=False
                ):
                    raise click.Abort()
            report = apply_report(report)
        logger.report(report, output_format)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        logger.error(
            "Discovery refused invalid metadata or unsafe paths. Repair the input and retry."
        )
        logger.verbose_detail(type(exc).__name__)
        raise click.ClickException("No onboarding changes were applied.") from exc


@click.command(hidden=True, help="Alias for 'apm init --discover'.")
@discover_options
@click.option("--yes", "-y", is_flag=True, help="Consent to the manifest delta")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed diagnostics")
def discover(write: bool, output_format: str, global_: bool, yes: bool, verbose: bool) -> None:
    """Invoke discovery without the init scaffold."""
    run_discover(
        write=write, output_format=output_format, global_=global_, yes=yes, verbose=verbose
    )
