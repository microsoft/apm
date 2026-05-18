"""``apm marketplace publish`` command."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.pr_integration import PrIntegrator
from ...marketplace.publisher import MarketplacePublisher
from .._helpers import _is_interactive
from . import marketplace
from . import publish_flow as _publish_flow
from ._io import _load_config_or_exit
from ._publish_helpers import _render_publish_plan, _render_publish_summary
from .publish_flow import (
    _build_pr_results,
    _confirm_publish,
    _exit_for_publish_results,
    _load_publish_targets,
    _PrBuildOpts,
    _render_state_path,
    _require_pr_integrator,
    _resolve_targets_path,
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
def publish(**kwargs):
    """Publish marketplace updates to consumer repositories."""
    _publish_flow.PrIntegrator = PrIntegrator
    _publish_flow._is_interactive = _is_interactive
    targets_file = kwargs["targets_file"]
    dry_run = kwargs["dry_run"]
    no_pr = kwargs["no_pr"]
    draft = kwargs["draft"]
    allow_downgrade = kwargs["allow_downgrade"]
    allow_ref_change = kwargs["allow_ref_change"]
    parallel = kwargs["parallel"]
    yes = kwargs["yes"]
    verbose = kwargs["verbose"]
    logger = CommandLogger("marketplace-publish", verbose=verbose)
    _load_config_or_exit(logger)

    mkt_json_path = Path.cwd() / "marketplace.json"
    if not mkt_json_path.exists():
        logger.error("marketplace.json not found. Run 'apm pack' first.", symbol="error")
        sys.exit(1)

    targets_path = _resolve_targets_path(targets_file, logger)
    targets = _load_publish_targets(targets_path, logger)
    pr = _require_pr_integrator(no_pr, logger)

    publisher = MarketplacePublisher(Path.cwd())
    plan = publisher.plan(
        targets,
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
    )
    _render_publish_plan(logger, plan)
    _confirm_publish(targets, yes, dry_run, logger)

    results = publisher.execute(plan, dry_run=dry_run, parallel=parallel)
    pr_results = _build_pr_results(
        pr, results, plan, _PrBuildOpts(dry_run=dry_run, draft=draft, no_pr=no_pr)
    )
    _render_publish_summary(logger, results, pr_results, no_pr, dry_run)
    _render_state_path(logger)
    _exit_for_publish_results(results)
