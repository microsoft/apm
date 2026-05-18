"""Flow helpers for ``apm marketplace publish``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import click

from ...marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ...marketplace.publisher import PublishOutcome
from .._helpers import _get_console, _is_interactive
from ._publish_helpers import _load_targets_file


def _resolve_targets_path(targets_file, logger) -> Path:
    """Resolve and validate the consumer targets file path."""
    if targets_file:
        targets_path = Path(targets_file)
        if targets_path.exists():
            return targets_path
        logger.error(f"Targets file not found: {targets_file}", symbol="error")
        sys.exit(1)

    targets_path = Path.cwd() / "consumer-targets.yml"
    if targets_path.exists():
        return targets_path

    logger.error(
        "No consumer-targets.yml found. Create one or pass --targets <path>.\n"
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


def _load_publish_targets(targets_path: Path, logger):
    """Load and validate publish targets from YAML."""
    targets, error = _load_targets_file(targets_path)
    if not error:
        return targets
    logger.error(error, symbol="error")
    sys.exit(1)


def _require_pr_integrator(no_pr: bool, logger) -> PrIntegrator | None:
    """Return a ready PR integrator unless ``--no-pr`` disabled it."""
    if no_pr:
        return None
    pr = PrIntegrator()
    available, hint = pr.check_available()
    if available:
        return pr
    logger.error(hint, symbol="error")
    sys.exit(1)


def _confirm_publish(targets, yes: bool, dry_run: bool, logger) -> None:
    """Handle publish confirmation and dry-run messaging."""
    if not yes:
        if not _is_interactive():
            logger.error(
                "Non-interactive session: pass --yes to confirm the publish.",
                symbol="error",
            )
            sys.exit(1)
        try:
            if not click.confirm(f"Confirm publish to {len(targets)} repositories?", default=False):
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


@dataclass(frozen=True, slots=True)
class _PrBuildOpts:
    """Options controlling PR creation in :func:`_build_pr_results`."""

    dry_run: bool
    draft: bool
    no_pr: bool


def _build_pr_results(pr: PrIntegrator | None, results, plan, opts: _PrBuildOpts):
    """Build PR results for publish outcomes."""
    if opts.no_pr:
        return []
    if pr is None:
        pr = PrIntegrator()

    pr_results = []
    for result in results:
        if result.outcome == PublishOutcome.UPDATED:
            pr_results.append(
                pr.open_or_update(
                    plan,
                    result.target,
                    result,
                    no_pr=False,
                    draft=opts.draft,
                    dry_run=opts.dry_run,
                )
            )
            continue
        pr_results.append(
            PrResult(
                target=result.target,
                state=PrState.SKIPPED,
                pr_number=None,
                pr_url=None,
                message=f"No PR needed: {result.outcome.value}",
            )
        )
    return pr_results


def _render_state_path(logger) -> None:
    """Render the publish state file path without awkward terminal wrapping."""
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
            return
    except Exception:
        pass
    logger.progress(f"State file: {state_path}", symbol="info")


def _exit_for_publish_results(results) -> None:
    """Exit non-zero when any publish target failed."""
    failed_count = sum(1 for result in results if result.outcome == PublishOutcome.FAILED)
    if failed_count > 0:
        sys.exit(1)
