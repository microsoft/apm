"""``apm marketplace init`` command."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from . import (
    _check_gitignore_for_marketplace_json,
    marketplace,
)


@marketplace.command(help="Add a 'marketplace:' block to apm.yml (scaffolds apm.yml if missing)")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing 'marketplace:' block in apm.yml",
)
@click.option(
    "--no-gitignore-check",
    is_flag=True,
    help="Skip the .gitignore staleness check",
)
@click.option("--name", default=None, help="Marketplace/package name (default: my-marketplace)")
@click.option("--owner", default=None, help="Owner name for the marketplace")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def init(force, no_gitignore_check, name, owner, verbose):
    """Scaffold a ``marketplace:`` block in apm.yml (creates apm.yml if absent)."""
    from ruamel.yaml import YAML

    from ...marketplace.init_template import render_marketplace_block

    logger = CommandLogger("marketplace-init", verbose=verbose)
    apm_path = Path.cwd() / "apm.yml"
    scaffolded_apm_yml = False

    if not apm_path.exists():
        scaffold_name = name or "my-marketplace"
        scaffold_text = (
            f"name: {scaffold_name}\n"
            "version: 0.1.0\n"
            "description: A short description of what this repo offers\n"
        )
        try:
            apm_path.write_text(scaffold_text, encoding="utf-8")
        except OSError as exc:
            logger.error(f"Failed to write apm.yml: {exc}", symbol="error")
            sys.exit(1)
        scaffolded_apm_yml = True
        if verbose:
            logger.verbose_detail(f"    Path: {apm_path}")

    try:
        rt = YAML(typ="rt")
        rt.preserve_quotes = True
        rt.indent(mapping=2, sequence=4, offset=2)
        data = rt.load(apm_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to parse apm.yml: {exc}", symbol="error")
        sys.exit(1)

    if data is None:
        from ruamel.yaml.comments import CommentedMap

        data = CommentedMap()
    elif not isinstance(data, dict):
        logger.error(
            f"apm.yml must be a YAML mapping at the top level (got {type(data).__name__}).",
            symbol="error",
        )
        sys.exit(1)

    if "marketplace" in data and data["marketplace"] is not None and not force:
        logger.warning(
            "apm.yml already has a 'marketplace:' block. Use --force to overwrite.",
            symbol="warning",
        )
        sys.exit(1)

    block_data = rt.load(render_marketplace_block(owner=owner))
    data["marketplace"] = block_data["marketplace"]

    out = StringIO()
    rt.dump(data, out)
    try:
        apm_path.write_text(out.getvalue(), encoding="utf-8")
    except OSError as exc:
        logger.error(f"Failed to write apm.yml: {exc}", symbol="error")
        sys.exit(1)

    if scaffolded_apm_yml:
        logger.success("Created apm.yml with 'marketplace:' block", symbol="check")
    else:
        logger.success("Added 'marketplace:' block to apm.yml", symbol="check")

    if verbose:
        logger.verbose_detail(f"    Path: {apm_path}")

    # .gitignore staleness check
    if not no_gitignore_check:
        _check_gitignore_for_marketplace_json(logger)

    # Next steps panel
    next_steps = [
        "Edit the 'marketplace:' block in apm.yml to add your packages",
        "Run 'apm pack' to generate .claude-plugin/marketplace.json",
        "Commit BOTH apm.yml and the generated marketplace.json",
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
