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
    help=(
        "Show resolved targets for the current project. "
        "If APM detects a target you don't intend (e.g. CLAUDE.md is documentation, "
        "not a Claude Code config), pin your targets explicitly in apm.yml."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON instead of a table.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Include the agent-skills meta-target in JSON output (excluded by default).",
)
@click.pass_context
def targets(ctx: click.Context, *, as_json: bool, show_all: bool) -> None:
    """Show resolved targets for the current project."""
    if ctx.invoked_subcommand is not None:
        return  # delegate to sub-command

    from apm_cli.core.errors import AmbiguousHarnessError, NoHarnessError
    from apm_cli.core.target_detection import (
        CANONICAL_DEPLOY_DIRS,
        CANONICAL_SIGNAL,
        CANONICAL_TARGETS_ORDERED,
        detect_signals,
        resolve_targets,
    )

    project_root = Path.cwd()
    # agent-skills is a meta-target (multi-harness fan-out), not a
    # harness in itself. Excluded from the apm targets table; visible
    # only in JSON output if invoked with --all (convergence item 13).

    # Try to resolve targets using the v2 algorithm.
    # On ambiguous-harness, show all detected signals (the user ran
    # ``apm targets`` precisely to see what's there).
    # On no-harness error, report empty active list.
    try:
        resolved = resolve_targets(project_root)
        active = resolved.targets
    except AmbiguousHarnessError:
        signals = detect_signals(project_root)
        active = sorted({s.target for s in signals})
    except (NoHarnessError, click.UsageError):
        active = []

    # Hoist signal scan out of the per-row loop -- detect_signals walks the
    # whole signal whitelist on every call, so doing it inside _row turned a
    # 7-target render into 7x filesystem scans for no extra information.
    all_signals = detect_signals(project_root)

    # Build per-target row data from canonical tables.
    def _row(name: str) -> dict:
        is_active = name in active
        active_source = next((s.source for s in all_signals if s.target == name), None)
        return {
            "target": name,
            "status": "active" if is_active else "inactive",
            "source": active_source if is_active else None,
            "deploy_dir": CANONICAL_DEPLOY_DIRS.get(name, "?"),
            "needs": None if is_active else CANONICAL_SIGNAL.get(name),
        }

    rows = [_row(name) for name in CANONICAL_TARGETS_ORDERED]

    if as_json:
        if show_all:
            # Surface meta-target only when explicitly requested.
            rows = [
                *rows,
                {
                    "target": "agent-skills",
                    "status": "active" if "agent-skills" in active else "inactive",
                    "source": None,
                    "deploy_dir": ".agents/",
                    "needs": None,
                    "meta_target": True,
                },
            ]
        click.echo(_json.dumps(rows, indent=2))
        return

    # Table output. Inactive rows show 'needs <path>' so the recovery
    # path is self-documenting (convergence item 6).
    click.echo(f"  {'TARGET':<12} {'STATUS':<10} {'SOURCE':<40} {'DEPLOY DIR'}")
    click.echo(f"  {'-' * 12} {'-' * 10} {'-' * 40} {'-' * 10}")
    for row in rows:
        if row["status"] == "active":
            source_col = row["source"] or ""
        else:
            source_col = f"needs {row['needs']}" if row["needs"] else ""
        click.echo(
            f"  {row['target']:<12} {row['status']:<10} {source_col:<40} {row['deploy_dir']}"
        )

    if not active:
        from apm_cli.utils.console import _rich_info

        click.echo("")
        _rich_info(
            "Create a harness config (e.g. CLAUDE.md, .cursor/, .github/copilot-instructions.md) "
            "or declare `targets:` in apm.yml.",
            symbol="info",
        )
