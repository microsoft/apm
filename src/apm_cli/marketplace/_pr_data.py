"""PR data models and inner logic for :mod:`pr_integration`.

Extracted from :mod:`pr_integration` to keep that module under 400 lines.
Public names (``PrState``, ``PrResult``) continue to be importable from
:mod:`apm_cli.marketplace.pr_integration` via explicit re-exports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .publisher import ConsumerTarget, PublishPlan


class PrState(str, Enum):
    """Outcome of a PR operation on a single consumer target."""

    OPENED = "opened"  # new PR created
    UPDATED = "updated"  # existing PR for the branch already open
    SKIPPED = "skipped"  # no update needed (non-UPDATED outcome)
    FAILED = "failed"  # gh call failed
    DISABLED = "disabled"  # --no-pr was set for this target


@dataclass(frozen=True)
class PrResult:
    """Result of a PR operation on a single consumer target."""

    target: ConsumerTarget
    state: PrState
    pr_number: int | None  # set when OPENED or UPDATED
    pr_url: str | None  # set when OPENED or UPDATED
    message: str  # human-readable detail


# ---------------------------------------------------------------------------
# PR URL parsing
# ---------------------------------------------------------------------------

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _extract_short_hash(plan: PublishPlan) -> str:
    """Return the short hash from *plan*, falling back to the branch name.

    The branch name is ``apm/marketplace-update-{name}-{ver}-{hash}``
    so the hash is the last segment after the final ``-``.
    """
    if plan.short_hash:
        return plan.short_hash
    # Derive from branch_name -- it ends with "-{short_hash}"
    parts = plan.branch_name.rsplit("-", 1)
    if len(parts) == 2:
        return parts[1]
    return ""


def _build_title(plan: PublishPlan) -> str:
    """Build the PR title."""
    return f"chore(apm): bump {plan.marketplace_name} to {plan.marketplace_version}"


def _build_body(plan: PublishPlan, target: ConsumerTarget) -> str:
    """Build the PR body."""
    short_hash = _extract_short_hash(plan)
    return (
        f"Automated update from `apm marketplace publish`.\n"
        f"\n"
        f"- Marketplace: `{plan.marketplace_name}`\n"
        f"- New version: `{plan.marketplace_version}`\n"
        f"- New ref: `{plan.new_ref}`\n"
        f"- Branch: `{plan.branch_name}`\n"
        f"\n"
        f"This PR updates `dependencies.apm` entries that reference "
        f"`{plan.marketplace_name}` "
        f"in `{target.path_in_repo}`.\n"
        f"\n"
        f"<!-- APM-Publish-Id: {short_hash} -->\n"
    )


# ---------------------------------------------------------------------------
# Inner PR open/update logic (free function; called with self as first arg)
# ---------------------------------------------------------------------------


def _open_or_update_inner(
    self,
    plan: PublishPlan,
    target: ConsumerTarget,
    *,
    draft: bool = False,
    dry_run: bool = False,
) -> PrResult:
    """Core logic for open_or_update, without error handling."""
    # 1. Check for existing PR
    existing = self._find_existing_pr(plan, target)

    title = _build_title(plan)
    body = _build_body(plan, target)

    if existing is not None:
        # Existing PR found
        pr_number = existing["number"]
        pr_url = existing["url"]
        existing_body = existing.get("body", "")

        if body == existing_body:
            return PrResult(
                target=target,
                state=PrState.UPDATED,
                pr_number=pr_number,
                pr_url=pr_url,
                message="PR already open, body unchanged.",
            )

        # Update the PR body
        self._update_pr_body(target, pr_number, body)
        return PrResult(
            target=target,
            state=PrState.UPDATED,
            pr_number=pr_number,
            pr_url=pr_url,
            message="PR body updated.",
        )

    # 2. No existing PR -- create
    if dry_run:
        return PrResult(
            target=target,
            state=PrState.OPENED,
            pr_number=None,
            pr_url=None,
            message="[dry-run] Would open PR.",
        )

    pr_url, pr_number = self._create_pr(
        plan,
        target,
        title,
        body,
        draft=draft,
    )

    return PrResult(
        target=target,
        state=PrState.OPENED,
        pr_number=pr_number,
        pr_url=pr_url,
        message="PR opened.",
    )
