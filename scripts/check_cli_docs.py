#!/usr/bin/env python3
"""Compare public top-level Click commands with rendered CLI reference pages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import click

from apm_cli.cli import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = REPO_ROOT / "docs" / "dist"


def recovery_guidance(dist_dir: Path, *, mismatch: bool) -> str:
    """Return root-safe recovery guidance for the selected output tree."""
    if dist_dir.resolve() == DEFAULT_DIST.resolve():
        rebuild = "rebuild with 'npm --prefix docs run build'"
    else:
        rebuild = f"rebuild the rendered docs at '{dist_dir}'"
    if mismatch:
        action = f"Add or remove the matching CLI reference page, {rebuild}"
    else:
        action = rebuild[0].upper() + rebuild[1:]
    return (
        f"[i] {action}, then rerun 'uv run --frozen python scripts/check_cli_docs.py {dist_dir}'."
    )


def public_top_level_commands(group: click.Group) -> set[str]:
    """Return visible top-level names from Click's live command registry."""
    return {name for name, command in group.commands.items() if not command.hidden}


def rendered_cli_reference_pages(dist_dir: Path) -> set[str]:
    """Return one-level CLI pages emitted by the Astro build."""
    cli_dir = dist_dir / "reference" / "cli"
    if not cli_dir.is_dir():
        raise FileNotFoundError(f"rendered CLI directory not found: {cli_dir}")

    return {
        child.name
        for child in cli_dir.iterdir()
        if child.is_dir() and (child / "index.html").is_file()
    }


def registry_docs_mismatches(
    group: click.Group,
    dist_dir: Path,
) -> tuple[list[str], list[str]]:
    """Return missing rendered pages and rendered pages without commands."""
    commands = public_top_level_commands(group)
    pages = rendered_cli_reference_pages(dist_dir)
    return sorted(commands - pages), sorted(pages - commands)


def main(argv: list[str] | None = None) -> int:
    """Validate the repository CLI registry against one rendered dist tree."""
    parser = argparse.ArgumentParser(
        description="Check public CLI commands against rendered reference pages."
    )
    parser.add_argument(
        "dist_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_DIST,
        help="Astro output directory (default: docs/dist)",
    )
    args = parser.parse_args(argv)

    try:
        missing_pages, orphan_pages = registry_docs_mismatches(
            cli,
            args.dist_dir,
        )
    except FileNotFoundError as error:
        print(f"[x] {error}", file=sys.stderr)
        print(recovery_guidance(args.dist_dir, mismatch=False), file=sys.stderr)
        return 1

    if missing_pages or orphan_pages:
        print("[x] CLI registry/rendered documentation mismatch:", file=sys.stderr)
        if missing_pages:
            print(
                "  executable commands missing rendered pages: " + ", ".join(missing_pages),
                file=sys.stderr,
            )
        if orphan_pages:
            print(
                "  rendered pages missing executable commands: " + ", ".join(orphan_pages),
                file=sys.stderr,
            )
        print(recovery_guidance(args.dist_dir, mismatch=True), file=sys.stderr)
        return 1

    command_count = len(public_top_level_commands(cli))
    print(f"[+] {command_count} public CLI commands match {command_count} rendered pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
