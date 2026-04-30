"""``apm marketplace publish`` command."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ...marketplace.publisher import MarketplacePublisher, PublishOutcome
from .._helpers import _get_console, _is_interactive
from . import (
    _load_config_or_exit,
    _load_targets_file,
    _render_publish_plan,
    _render_publish_summary,
    marketplace,
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
    targets_file,
    dry_run,
    no_pr,
    draft,
    allow_downgrade,
    allow_ref_change,
    parallel,
    yes,
    verbose,
):
    """Publish marketplace updates to consumer repositories."""
    logger = CommandLogger("marketplace-publish", verbose=verbose)

    # ------------------------------------------------------------------
    # 1. Pre-flight checks
    # ------------------------------------------------------------------

    # 1a. Load marketplace authoring config
    _load_config_or_exit(logger)

    # 1b. Load marketplace.json
    mkt_json_path = Path.cwd() / "marketplace.json"
    if not mkt_json_path.exists():
        logger.error(
            "marketplace.json not found. Run 'apm pack' first.",
            symbol="error",
        )
        sys.exit(1)

    # 1c. Load targets
    if targets_file:
        targets_path = Path(targets_file)
        if not targets_path.exists():
            logger.error(
                f"Targets file not found: {targets_file}",
                symbol="error",
            )
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

    # 1d. Check gh availability (unless --no-pr)
    pr = None
    if not no_pr:
        pr = PrIntegrator()
        available, hint = pr.check_available()
        if not available:
            logger.error(hint, symbol="error")
            sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Plan and confirm
    # ------------------------------------------------------------------

    publisher = MarketplacePublisher(Path.cwd())
    plan = publisher.plan(
        targets,
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
    )

    # Render publish plan
    _render_publish_plan(logger, plan)

    # Confirmation logic
    if not yes:
        if not _is_interactive():
            logger.error(
                "Non-interactive session: pass --yes to confirm the publish.",
                symbol="error",
            )
            sys.exit(1)
        try:
            if not click.confirm(
                f"Confirm publish to {len(targets)} repositories?",
                default=False,
            ):
                logger.progress("Publish cancelled.", symbol="info")
                sys.exit(0)
        except click.Abort:
            logger.progress("Publish cancelled.", symbol="info")
            sys.exit(0)

    if dry_run:
        logger.progress(
            "Dry run: no branches will be pushed and no PRs will be opened.",
            symbol="info",
        )

    # ------------------------------------------------------------------
    # 3. Execute publish
    # ------------------------------------------------------------------

    results = publisher.execute(plan, dry_run=dry_run, parallel=parallel)

    # PR integration
    pr_results = []
    if not no_pr:
        if pr is None:
            pr = PrIntegrator()

        for result in results:
            if dry_run:
                # In dry-run, preview what PR would do for UPDATED targets
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
            else:  # noqa: PLR5501
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

    # ------------------------------------------------------------------
    # 4. Summary rendering
    # ------------------------------------------------------------------

    _render_publish_summary(logger, results, pr_results, no_pr, dry_run)

    # State file path -- use soft_wrap so the path is never split mid-word
    # in narrow terminals (Rich would otherwise break at hyphens).
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
            logger.progress(f"State file: {state_path}", symbol="info")
    except Exception:
        logger.progress(f"State file: {state_path}", symbol="info")

    # Exit code
    failed_count = sum(1 for r in results if r.outcome == PublishOutcome.FAILED)
    if failed_count > 0:
        sys.exit(1)
