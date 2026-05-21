"""Publish command rendering and target-file helpers."""

from __future__ import annotations

import click
import yaml

from ...marketplace.publisher import ConsumerTarget, PublishOutcome
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console


def _load_targets_file(path):
    """Load and validate a consumer-targets YAML file.

    Returns a list of ``ConsumerTarget`` instances.

    Raises ``SystemExit`` on validation failures.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, f"Invalid YAML in targets file: {exc}"
    except OSError as exc:
        return None, f"Cannot read targets file: {exc}"

    if not isinstance(raw, dict) or "targets" not in raw:
        return None, "Targets file must contain a 'targets' key."

    raw_targets = raw["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        return None, "Targets file must contain a non-empty 'targets' list."

    targets = []
    for idx, entry in enumerate(raw_targets):
        if not isinstance(entry, dict):
            return None, f"targets[{idx}] must be a mapping."

        repo = entry.get("repo")
        if not repo or not isinstance(repo, str):
            return None, f"targets[{idx}]: 'repo' is required (owner/name)."

        # Validate repo format: owner/name
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None, f"targets[{idx}]: 'repo' must be 'owner/name', got '{repo}'."

        branch = entry.get("branch")
        if not branch or not isinstance(branch, str):
            return None, f"targets[{idx}]: 'branch' is required."

        path_in_repo = entry.get("path_in_repo", "apm.yml")
        if not isinstance(path_in_repo, str) or not path_in_repo.strip():
            return None, f"targets[{idx}]: 'path_in_repo' must be a non-empty string."

        # Path safety check
        try:
            validate_path_segments(
                path_in_repo,
                context=f"targets[{idx}].path_in_repo",
            )
        except PathTraversalError as exc:
            return None, str(exc)

        targets.append(
            ConsumerTarget(
                repo=repo.strip(),
                branch=branch.strip(),
                path_in_repo=path_in_repo.strip(),
            )
        )

    return targets, None


def _render_publish_plan(logger, plan):
    """Render the publish plan as a Rich panel + target table."""
    console = _get_console()

    plan_text = (
        f"Marketplace: {plan.marketplace_name}\n"
        f"New version: {plan.marketplace_version}\n"
        f"New ref:     {plan.new_ref}\n"
        f"Branch:      {plan.branch_name}\n"
        f"Targets:     {len(plan.targets)}"
    )

    if not console:
        logger.progress("Publish plan:", symbol="info")
        for line in plan_text.splitlines():
            logger.tree_item(f"  {line}")
        click.echo()
        for t in plan.targets:
            logger.tree_item(f"  [*] {t.repo}  branch={t.branch}  path={t.path_in_repo}")
        return

    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console.print()
    console.print(
        Panel(
            plan_text,
            title="Publish plan",
            border_style="cyan",
        )
    )

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Repo", style="bold white", no_wrap=True)
    table.add_column("Branch", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Status", no_wrap=True, width=10)

    for t in plan.targets:
        table.add_row(t.repo, t.branch, t.path_in_repo, Text("[*]"))

    console.print(table)
    console.print()


def _render_publish_summary(logger, results, pr_results, no_pr, dry_run):
    """Render the final publish summary table."""
    console = _get_console()

    # Build lookup for PR results by repo
    pr_by_repo = {}
    for pr_r in pr_results:
        pr_by_repo[pr_r.target.repo] = pr_r

    updated_count = sum(1 for r in results if r.outcome == PublishOutcome.UPDATED)
    failed_count = sum(1 for r in results if r.outcome == PublishOutcome.FAILED)
    total = len(results)

    if not console:
        click.echo()
        for r in results:
            icon = _outcome_symbol(r.outcome)
            pr_info = ""
            if not no_pr:
                pr_r = pr_by_repo.get(r.target.repo)
                if pr_r:
                    pr_info = f"  PR: {pr_r.state.value}"
                    if pr_r.pr_number:
                        pr_info += f" #{pr_r.pr_number}"
            logger.tree_item(f"  {icon} {r.target.repo}: {r.outcome.value}{pr_info} -- {r.message}")
        click.echo()
        _render_publish_footer(logger, updated_count, failed_count, total, dry_run)
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Publish Results",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Repo", style="bold white", no_wrap=True)
    table.add_column("Outcome", style="white")

    if not no_pr:
        table.add_column("PR State", style="white")
        table.add_column("PR #", style="cyan", justify="right")
        table.add_column("PR URL", style="dim")

    table.add_column("Message", style="dim", ratio=1)

    for r in results:
        icon = _outcome_symbol(r.outcome)
        row = [Text(icon), r.target.repo, r.outcome.value]

        if not no_pr:
            pr_r = pr_by_repo.get(r.target.repo)
            if pr_r:
                row.append(pr_r.state.value)
                row.append(str(pr_r.pr_number) if pr_r.pr_number else "--")
                row.append(pr_r.pr_url or "--")
            else:
                row.extend(["--", "--", "--"])

        row.append(r.message)
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print()

    _render_publish_footer(logger, updated_count, failed_count, total, dry_run)


def _outcome_symbol(outcome):
    """Map a ``PublishOutcome`` to a bracket symbol."""
    if outcome == PublishOutcome.UPDATED:
        return "[+]"
    elif outcome == PublishOutcome.FAILED:
        return "[x]"
    elif outcome in (
        PublishOutcome.SKIPPED_DOWNGRADE,
        PublishOutcome.SKIPPED_REF_CHANGE,
    ):
        return "[!]"
    elif outcome == PublishOutcome.NO_CHANGE:
        return "[*]"
    return "[*]"


def _render_publish_footer(logger, updated, failed, total, dry_run):
    """Render the footer success/warning line."""
    suffix = " (dry-run)" if dry_run else ""
    if failed == 0:
        logger.success(
            f"Published {updated}/{total} targets{suffix}",
            symbol="check",
        )
    else:
        logger.warning(
            f"Published {updated}/{total} targets, {failed} failed{suffix}",
            symbol="warning",
        )
