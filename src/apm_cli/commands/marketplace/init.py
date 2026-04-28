"""``apm marketplace init`` command."""

from __future__ import annotations

import sys
from pathlib import Path

import click


from . import (marketplace, _require_authoring_flag, _check_gitignore_for_marketplace_json, CommandLogger)

@marketplace.command(help="Scaffold a new marketplace.yml in the current directory")
@click.option("--force", is_flag=True, help="Overwrite existing marketplace.yml")
@click.option(
    "--no-gitignore-check",
    is_flag=True,
    help="Skip the .gitignore staleness check",
)
@click.option("--name", default=None, help="Marketplace name (default: my-marketplace)")
@click.option("--owner", default=None, help="Owner name for the marketplace")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def init(force, no_gitignore_check, name, owner, verbose):
    """Create a richly-commented marketplace.yml scaffold."""
    _require_authoring_flag()
    from ...marketplace.init_template import render_marketplace_yml_template

    logger = CommandLogger("marketplace-init", verbose=verbose)
    yml_path = Path.cwd() / "marketplace.yml"

    # Guard: file already exists
    if yml_path.exists() and not force:
        logger.error(
            "marketplace.yml already exists. Use --force to overwrite.",
            symbol="error",
        )
        sys.exit(1)

    # Write template
    template_text = render_marketplace_yml_template(name=name, owner=owner)
    try:
        yml_path.write_text(template_text, encoding="utf-8")
    except OSError as exc:
        logger.error(f"Failed to write marketplace.yml: {exc}", symbol="error")
        sys.exit(1)

    logger.success("Created marketplace.yml", symbol="check")

    if verbose:
        logger.verbose_detail(f"    Path: {yml_path}")

    # .gitignore staleness check
    if not no_gitignore_check:
        _check_gitignore_for_marketplace_json(logger)

    # Next steps panel
    next_steps = [
        "Edit marketplace.yml to add your packages",
        "Run 'apm marketplace build' to generate marketplace.json",
        "Commit BOTH marketplace.yml and marketplace.json",
    ]

    try:
        from ...utils.console import _rich_panel

        _rich_panel(
            "\n".join(f"  {i}. {step}" for i, step in enumerate(next_steps, 1)),
            title=" Next Steps",
            style="cyan",
        )
    except (ImportError, NameError):
        logger.progress("Next steps:")
        for i, step in enumerate(next_steps, 1):
            logger.tree_item(f"  {i}. {step}")
