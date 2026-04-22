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

from .discovery import PolicyFetchResult, discover_policy_with_chain
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


# Maximum lines to emit per severity bucket in dry-run preview.
# Overflow is collapsed into a single tail line pointing to ``apm audit``.
_DRY_RUN_PREVIEW_LIMIT = 5


def run_policy_preflight(
    *,
    project_root: Path,
    apm_deps=None,
    mcp_deps=None,
    no_policy: bool = False,
    logger,
    dry_run: bool = False,
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
        ``policy_disabled``, ``policy_resolved``, ``policy_violation``,
        ``warning``).
    dry_run:
        When ``True``, run discovery and checks but emit preview-style
        verdicts instead of raising :class:`PolicyBlockError`.
        Block-severity violations render as
        ``"[!] Would be blocked by policy: <dep> -- <reason>"``
        and warn-severity as ``"[!] Policy warning: <dep> -- <reason>"``.
        The function always returns normally in dry-run mode.

    Returns
    -------
    (PolicyFetchResult | None, enforcement_active: bool)
        ``enforcement_active`` is ``True`` when a policy was found and
        its enforcement level is ``"warn"`` or ``"block"``.

    Raises
    ------
    PolicyBlockError
        When ``enforcement == "block"`` and at least one check fails
        **and** ``dry_run is False``.
        The caller should abort the install and exit non-zero.
    """
    # -- Escape hatches ------------------------------------------------
    if no_policy or os.environ.get("APM_POLICY_DISABLE") == "1":
        reason = "--no-policy" if no_policy else "APM_POLICY_DISABLE=1"
        logger.policy_disabled(reason)
        return None, False

    # -- Discovery (chain-aware: resolves extends: + merges) -----------
    fetch_result = discover_policy_with_chain(project_root)

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
        if dry_run:
            # -- D2: capped preview per severity bucket ----------------
            block_lines: list[tuple[str, str]] = []
            warn_lines: list[tuple[str, str]] = []
            for check in audit_result.failed_checks:
                for detail in check.details:
                    dep_ref = detail.split(":")[0].strip() if ":" in detail else detail
                    if enforcement == "block":
                        block_lines.append((dep_ref, detail))
                    else:
                        warn_lines.append((dep_ref, detail))

            # Emit block bucket (capped)
            for dep_ref, detail in block_lines[:_DRY_RUN_PREVIEW_LIMIT]:
                logger.warning(
                    f"Would be blocked by policy: {dep_ref} -- {detail}"
                )
            overflow = len(block_lines) - _DRY_RUN_PREVIEW_LIMIT
            if overflow > 0:
                logger.warning(
                    f"... and {overflow} more would be blocked by policy. "
                    "Run `apm audit` for full report."
                )

            # Emit warn bucket (capped)
            for dep_ref, detail in warn_lines[:_DRY_RUN_PREVIEW_LIMIT]:
                logger.warning(
                    f"Policy warning: {dep_ref} -- {detail}"
                )
            overflow = len(warn_lines) - _DRY_RUN_PREVIEW_LIMIT
            if overflow > 0:
                logger.warning(
                    f"... and {overflow} more policy warnings. "
                    "Run `apm audit` for full report."
                )
        else:
            # -- Real install: push each violation to DiagnosticCollector
            for check in audit_result.failed_checks:
                for detail in check.details:
                    dep_ref = detail.split(":")[0].strip() if ":" in detail else detail
                    logger.policy_violation(
                        dep_ref=dep_ref,
                        reason=detail,
                        severity="block" if enforcement == "block" else "warn",
                    )

        if enforcement == "block" and not dry_run:
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
