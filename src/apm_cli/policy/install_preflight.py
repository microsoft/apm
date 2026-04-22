"""Pre-install policy enforcement for non-pipeline command sites.

Shared helper used by:
- ``install --mcp`` branch (W2-mcp-preflight)
- ``install <pkg>`` rollback (W2-pkg-rollback) -- imports this helper
- ``install --dry-run`` preflight (W2-dry-run) -- same helper, read-only mode

When ``install/phases/policy_gate.py`` lands (W2-gate-phase), it should
delegate to :func:`run_policy_preflight` for discovery + outcome logic
rather than duplicate it.  The gate phase adds pipeline-specific wiring
(writing ``ctx.policy_fetch``, ``ctx.policy_enforcement_active``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from .discovery import PolicyFetchResult, discover_policy
from .models import CIAuditResult
from .policy_checks import run_dependency_policy_checks
from .schema import ApmPolicy


class PolicyBlockError(Exception):
    """Raised when policy enforcement blocks installation.

    Attributes:
        audit_result: The :class:`CIAuditResult` containing failed checks.
        policy_source: Human-readable policy source for diagnostics.
    """

    def __init__(
        self, message: str, *, audit_result: CIAuditResult, policy_source: str
    ):
        super().__init__(message)
        self.audit_result = audit_result
        self.policy_source = policy_source


def run_policy_preflight(
    *,
    project_root: Path,
    apm_deps=None,
    mcp_deps=None,
    no_policy: bool = False,
    logger,
) -> Tuple[Optional[PolicyFetchResult], bool]:
    """Discover + enforce policy for a non-pipeline command site.

    Parameters
    ----------
    project_root:
        Project root directory (for policy discovery via git remote).
    apm_deps:
        Iterable of ``DependencyReference``, or ``None`` to skip APM
        dep checks.
    mcp_deps:
        Iterable of ``MCPDependency``, or ``None`` to skip MCP checks.
    no_policy:
        CLI ``--no-policy`` flag value.
    logger:
        An :class:`InstallLogger` (or any object exposing
        ``policy_disabled``, ``policy_resolved``, ``policy_violation``).

    Returns
    -------
    (PolicyFetchResult | None, enforcement_active: bool)
        ``enforcement_active`` is ``True`` when a policy was found and
        its enforcement level is ``"warn"`` or ``"block"``.

    Raises
    ------
    PolicyBlockError
        When ``enforcement == "block"`` and at least one check fails.
        The caller should abort the install and exit non-zero.
    """
    # -- Escape hatches ------------------------------------------------
    if no_policy or os.environ.get("APM_POLICY_DISABLE") == "1":
        reason = "--no-policy" if no_policy else "APM_POLICY_DISABLE=1"
        logger.policy_disabled(reason)
        return None, False

    # -- Discovery -----------------------------------------------------
    fetch_result = discover_policy(project_root)

    # Outcome routing (plan section B)
    if not fetch_result.found:
        _log_discovery_miss(fetch_result, logger)
        return fetch_result, False

    policy: ApmPolicy = fetch_result.policy  # type: ignore[assignment]
    enforcement = policy.enforcement

    # Log discovery success
    logger.policy_resolved(
        source=fetch_result.source,
        cached=fetch_result.cached,
        enforcement=enforcement,
        age_seconds=fetch_result.cache_age_seconds,
    )

    if enforcement == "off":
        return fetch_result, False

    # -- Enforcement (warn or block) -----------------------------------
    audit_result = run_dependency_policy_checks(
        apm_deps if apm_deps is not None else [],
        lockfile=None,
        policy=policy,
        mcp_deps=mcp_deps,
        fail_fast=(enforcement == "block"),
    )

    if not audit_result.passed:
        # Emit diagnostics via logger
        for check in audit_result.failed_checks:
            for detail in check.details:
                logger.policy_violation(
                    dep_ref=detail.split(":")[0].strip() if ":" in detail else detail,
                    reason=detail,
                    severity="block" if enforcement == "block" else "warn",
                )

        if enforcement == "block":
            raise PolicyBlockError(
                f"Policy enforcement blocked installation: "
                f"{len(audit_result.failed_checks)} check(s) failed",
                audit_result=audit_result,
                policy_source=fetch_result.source,
            )

    return fetch_result, True


def _log_discovery_miss(fetch_result: PolicyFetchResult, logger) -> None:
    """Emit the appropriate diagnostic for a non-found policy outcome."""
    from ..utils.console import _rich_info, _rich_warning

    outcome = fetch_result.outcome

    if outcome == "no_git_remote":
        _rich_warning(
            "Could not determine org from git remote; "
            "policy auto-discovery skipped",
            symbol="warning",
        )
    elif outcome == "disabled":
        # Already handled by the caller's no_policy check
        pass
    elif outcome == "malformed":
        _rich_warning(
            f"Policy at {fetch_result.source} is malformed "
            "-- contact your org admin to fix the policy file",
            symbol="warning",
        )
    elif outcome in ("cache_miss_fetch_fail", "garbage_response"):
        # Fail-open: warn loudly, never block (CEO ruling)
        _rich_warning(
            f"Could not fetch org policy ({fetch_result.error or 'unknown error'}) "
            "-- policy enforcement skipped for this invocation",
            symbol="warning",
        )
    elif outcome == "empty":
        _rich_warning(
            "Org policy is present but empty; no enforcement applied",
            symbol="warning",
        )
    elif outcome == "absent":
        _rich_info(
            f"No org policy found for {fetch_result.source or 'this project'}",
            symbol="info",
        )
    else:
        # Unknown outcome -- log conservatively
        if fetch_result.error:
            _rich_warning(
                f"Policy discovery issue: {fetch_result.error}",
                symbol="warning",
            )
