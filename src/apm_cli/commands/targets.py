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

from apm_cli.core.scope import manifest_file


def _manifest_targets(project_root: Path) -> list[str] | None:
    """Return the targets apm.yml restricts deployment to, read like install does."""
    from apm_cli.constants import APM_YML_FILENAME
    from apm_cli.models.apm_package import APMPackage, package_target_declaration

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return None
    try:
        declaration = package_target_declaration(APMPackage.from_apm_yml(manifest))
        if declaration.selection == "all":
            from apm_cli.core.target_catalog import expand_all

            # Singular ``target: all`` deploys the install set; restriction() folds it.
            return list(expand_all("install"))
        return declaration.restriction()
    except (OSError, ValueError) as exc:
        raise click.UsageError(f"Failed to read {APM_YML_FILENAME}: {exc}") from exc


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
    help="Include explicit-only targets (e.g. agent-skills, grok-bot) in JSON output (excluded by default).",
)
@click.pass_context
def targets(ctx: click.Context, *, as_json: bool, show_all: bool) -> None:
    """Show resolved targets for the current project."""
    if ctx.invoked_subcommand is not None:
        return  # delegate to sub-command

    from apm_cli.constants import APM_YML_FILENAME
    from apm_cli.core.errors import AmbiguousHarnessError, NoHarnessError
    from apm_cli.core.target_detection import (
        CANONICAL_DEPLOY_DIRS,
        CANONICAL_SIGNAL,
        CANONICAL_TARGETS_ORDERED,
        detect_signals,
        resolve_targets,
    )

    project_root = Path.cwd()
    yaml_targets = _manifest_targets(project_root)
    # Explicit-only targets (agent-skills, grok-bot, ...) are never
    # auto-detected, so they are excluded from the apm targets table and
    # visible only in JSON output with --all (convergence item 13).

    # Try to resolve targets using the v2 algorithm.
    # On ambiguous-harness, show all detected signals (the user ran
    # ``apm targets`` precisely to see what's there).
    # On no-harness error, report empty active list.
    manifest_source: str | None = None
    try:
        # Same precedence as install: apm.yml targets win over detected signals (#3792).
        resolved = resolve_targets(project_root, yaml_targets=yaml_targets)
        active = resolved.targets
        if resolved.source == APM_YML_FILENAME:
            manifest_source = resolved.source
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
    def _active_source(name: str) -> str | None:
        # Targets declared in apm.yml report the manifest, not a folder signal (#4210).
        if manifest_source is not None:
            return manifest_source
        return next((s.source for s in all_signals if s.target == name), None)

    def _row(name: str) -> dict:
        is_active = name in active
        active_source = _active_source(name)
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
            # Surface stable explicit-only targets (never auto-detected, so
            # absent from the default table) only when explicitly requested.
            # The set, deploy roots and meta-target flag come from the catalog.
            from apm_cli.core.target_catalog import TARGET_CAPABILITIES
            from apm_cli.integration.targets import KNOWN_TARGETS

            shown = {row["target"] for row in rows}  # architecture-authority-exempt: display-row
            for name, capability in TARGET_CAPABILITIES.items():
                profile = KNOWN_TARGETS.get(name)
                if (
                    not capability.explicit_only
                    or capability.experimental_flag is not None
                    or profile is None
                    or name in shown
                ):
                    continue
                rows = [
                    *rows,
                    {
                        "target": name,
                        "status": "active" if name in active else "inactive",
                        "source": _active_source(name) if name in active else None,
                        "deploy_dir": f"{profile.root_dir}/",
                        "needs": None,
                        "meta_target": capability.meta_target,
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
        # architecture-authority-exempt: display-row
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
