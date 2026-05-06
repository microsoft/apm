"""``apm targets`` -- inspect and manage target resolution.

A Click *group* so future sub-commands (e.g. ``apm targets add``) can be
added without breaking the top-level ``apm targets`` invocation.

When invoked without a sub-command (``apm targets``), prints the resolved
target list for the current project.  Flags:

* ``--json``  -- machine-readable JSON output
* ``--all``   -- include *every* canonical target, marking inactive ones

No provenance line is emitted (convergence override).
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click


@click.group(
    invoke_without_command=True,
    help="Show resolved targets for the current project.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all canonical targets, marking inactive ones.",
)
@click.pass_context
def targets(ctx: click.Context, *, as_json: bool, show_all: bool) -> None:
    """Show resolved targets for the current project."""
    if ctx.invoked_subcommand is not None:
        return  # delegate to sub-command

    from apm_cli.core.apm_yml import CANONICAL_TARGETS
    from apm_cli.core.errors import NoHarnessError
    from apm_cli.core.target_detection import resolve_targets
    from apm_cli.integration.targets import KNOWN_TARGETS

    project_root = Path.cwd()

    # Try to resolve targets using the v2 algorithm.
    # On no-harness error, report empty active list.
    try:
        resolved = resolve_targets(project_root)
        active = resolved.targets
        source = resolved.source
    except (NoHarnessError, click.UsageError):
        active = []
        source = "none"

    if show_all:
        # All canonical targets, with active/inactive status
        all_targets = sorted(CANONICAL_TARGETS)
        rows = []
        for name in all_targets:
            profile = KNOWN_TARGETS.get(name)
            rows.append(
                {
                    "name": name,
                    "active": name in active,
                    "root_dir": profile.root_dir if profile else "unknown",
                }
            )

        if as_json:
            click.echo(_json.dumps(rows, indent=2))
        else:
            for row in rows:
                marker = "*" if row["active"] else " "
                click.echo(f"  {marker} {row['name']:<20} {row['root_dir']}")
    elif as_json:
        click.echo(
            _json.dumps(
                {"targets": active, "source": source},
                indent=2,
            )
        )
    elif not active:
        click.echo("No targets detected.")
        click.echo(
            "Hint: create a harness config (e.g. CLAUDE.md, .cursor/, .github/copilot-instructions.md)"
        )
        click.echo("      or set 'target:' in apm.yml.")
    else:
        for name in active:
            profile = KNOWN_TARGETS.get(name)
            root = profile.root_dir if profile else "?"
            click.echo(f"  {name:<20} {root}/")
