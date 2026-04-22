"""``apm marketplace publish`` command."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.pr_integration import PrResult, PrState
from ...marketplace.publisher import ConsumerTarget, PublishOutcome
from . import (
    marketplace,
    _load_targets_file,
    _load_yml_or_exit,
    _render_publish_plan,
    _render_publish_summary,
)


@marketplace.command(help="Publish marketplace updates to consumer repositories")
@click.option(
    "--targets",
    "targets_file",
    default=None,
    type=click.Path(exists=False),
    help="Path to consumer-targets YAML file (default: ./consumer-targets.yml)",
)
@click.option("--dry-run", is_flag=True, help="Preview without pushing or opening PRs")
@click.option("--no-pr", is_flag=True, help="Push branches but skip PR creation")
@click.option("--draft", is_flag=True, help="Create PRs as drafts")
@click.option("--allow-downgrade", is_flag=True, help="Allow version downgrades")
@click.option("--allow-ref-change", is_flag=True, help="Allow switching ref types")
@click.option(
    "--parallel",
    default=4,
    show_default=True,
    type=int,
    help="Maximum number of concurrent target updates",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def publish(
    targets_file: str | None,
    dry_run: bool,
    no_pr: bool,
    draft: bool,
    allow_downgrade: bool,
    allow_ref_change: bool,
    parallel: int,
    yes: bool,
    verbose: bool,
) -> None:
    """Publish marketplace updates to consumer repositories."""
    from . import MarketplacePublisher, PrIntegrator, _get_console, _is_interactive

    logger = CommandLogger("marketplace-publish", verbose=verbose)

    _load_yml_or_exit(logger)

    mkt_json_path = Path.cwd() / "marketplace.json"
    if not mkt_json_path.exists():
        logger.error(
            "marketplace.json not found. Run 'apm marketplace build' first.",
            symbol="error",
        )
        sys.exit(1)

    if targets_file:
        targets_path = Path(targets_file)
        if not targets_path.exists():
            logger.error(f"Targets file not found: {targets_file}", symbol="error")
            sys.exit(1)
    else:
        targets_path = Path.cwd() / "consumer-targets.yml"
        if not targets_path.exists():
            logger.error(
                "No consumer-targets.yml found. "
                "Create one or pass --targets <path>.\n"
                "\n"
                "Example consumer-targets.yml:\n"
                "  targets:\n"
                "    - repo: acme-org/service-a\n"
                "      branch: main\n"
                "    - repo: acme-org/service-b\n"
                "      branch: develop",
                symbol="error",
            )
            sys.exit(1)

    targets, error = _load_targets_file(targets_path)
    if error:
        logger.error(error, symbol="error")
        sys.exit(1)
    if targets is None:
        logger.error("Failed to load publish targets.", symbol="error")
        sys.exit(1)

    resolved_targets: list[ConsumerTarget] = targets

    pr: PrIntegrator | None = None
    if not no_pr:
        pr = PrIntegrator()
        available, hint = pr.check_available()
        if not available:
            logger.error(hint, symbol="error")
            sys.exit(1)

    publisher = MarketplacePublisher(Path.cwd())
    plan = publisher.plan(
        resolved_targets,
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
    )

    _render_publish_plan(logger, plan)

    if not yes:
        if not _is_interactive():
            logger.error(
                "Non-interactive session: pass --yes to confirm the publish.",
                symbol="error",
            )
            sys.exit(1)
        answer = click.prompt(
            f"Confirm publish to {len(resolved_targets)} repositories? [y/N]",
            default="N",
            show_default=False,
        )
        if answer.strip().lower() != "y":
            logger.progress("Publish aborted by user.", symbol="info")
            return

    if dry_run:
        logger.progress(
            "Dry run: no branches will be pushed and no PRs will be opened.",
            symbol="info",
        )

    results = publisher.execute(plan, dry_run=dry_run, parallel=parallel)

    pr_results: list[PrResult] = []
    if not no_pr:
        if pr is None:
            pr = PrIntegrator()

        for result in results:
            if dry_run:
                if result.outcome == PublishOutcome.UPDATED:
                    pr_result = pr.open_or_update(
                        plan,
                        result.target,
                        result,
                        no_pr=False,
                        draft=draft,
                        dry_run=True,
                    )
                    pr_results.append(pr_result)
                else:
                    pr_results.append(
                        PrResult(
                            target=result.target,
                            state=PrState.SKIPPED,
                            pr_number=None,
                            pr_url=None,
                            message=f"No PR needed: {result.outcome.value}",
                        )
                    )
            else:
                if result.outcome == PublishOutcome.UPDATED:
                    pr_result = pr.open_or_update(
                        plan,
                        result.target,
                        result,
                        no_pr=False,
                        draft=draft,
                        dry_run=False,
                    )
                    pr_results.append(pr_result)
                else:
                    pr_results.append(
                        PrResult(
                            target=result.target,
                            state=PrState.SKIPPED,
                            pr_number=None,
                            pr_url=None,
                            message=f"No PR needed: {result.outcome.value}",
                        )
                    )

    _render_publish_summary(logger, results, pr_results, no_pr, dry_run)

    state_path = Path.cwd() / ".apm" / "publish-state.json"
    try:
        from rich.text import Text

        console = _get_console()
        if console is not None:
            console.print(
                Text(f"[i] State file: {state_path}", no_wrap=True),
                style="blue",
                highlight=False,
                soft_wrap=True,
            )
        else:
            click.echo(f"[i] State file: {state_path}")
    except Exception:
        click.echo(f"[i] State file: {state_path}")

    failed_count = sum(1 for result in results if result.outcome == PublishOutcome.FAILED)
    if failed_count > 0:
        sys.exit(1)
