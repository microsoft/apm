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
from typing import Optional, Tuple  # noqa: F401, UP035

# #832: Canonical exception type lives in ``apm_cli.install.errors``.
# ``PolicyBlockError`` remains as an alias re-exported below so external
# call sites that imported it from this module keep working.
from apm_cli.install.errors import PolicyViolationError

from .discovery import PolicyFetchResult, discover_policy_with_chain
from .models import CIAuditResult  # noqa: F401
from .outcome_routing import route_discovery_outcome
from .policy_checks import run_dependency_policy_checks
from .schema import ApmPolicy  # noqa: F401

# Deprecated alias kept for backward compatibility (#832).  New code
# should ``raise``/``except`` :class:`PolicyViolationError` directly.
PolicyBlockError = PolicyViolationError


# Maximum lines to emit per severity bucket in dry-run preview.
# Overflow is collapsed into a single tail line pointing to ``apm audit``.
_DRY_RUN_PREVIEW_LIMIT = 5


def _extract_dep_ref(detail: str, check_name: str) -> str:
    """Extract a dep ref from a ``CheckResult.details`` line.

    Contract: dependency-level checks in ``policy_checks.py`` produce
    detail lines of the form ``"{ref}: {reason}"`` (see e.g.
    ``_check_dependency_allowlist`` -- ``violations.append(f"{ref}: {reason}")``).
    Splitting on the first ``":"`` yields the ref family without the
    version suffix, which is what users want to see in the diagnostic.

    Defensively falls back to ``check_name`` when the detail string is
    empty or does not match the contract -- so a malformed check result
    still surfaces something identifying instead of an empty string.
    """
    if not detail:
        return check_name
    if ":" in detail:
        head = detail.split(":", 1)[0].strip()
        if head:
            return head
        # Pathological "leading colon" -- fall back to check_name
        # rather than returning the raw detail (which is just noise).
        return check_name
    return detail.strip() or check_name


def run_policy_preflight(
    *,
    project_root: Path,
    apm_deps=None,
    mcp_deps=None,
    no_policy: bool = False,
    logger,
    dry_run: bool = False,
) -> tuple[PolicyFetchResult | None, bool]:
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
        verdicts instead of raising :class:`PolicyViolationError`.
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
    PolicyViolationError
        When ``enforcement == "block"`` and at least one check fails
        **and** ``dry_run is False``.
        The caller should abort the install and exit non-zero.
        ``PolicyBlockError`` is a deprecated alias for the same class.
    """
    # -- Escape hatches ------------------------------------------------
    if no_policy or os.environ.get("APM_POLICY_DISABLE") == "1":
        reason = "--no-policy" if no_policy else "APM_POLICY_DISABLE=1"
        logger.policy_disabled(reason)
        return None, False

    # -- Discovery (chain-aware: resolves extends: + merges) -----------
    fetch_result = discover_policy_with_chain(project_root)

    # -- Route the outcome through the shared 9-outcome table ---------
    # Logging + fail-closed gating live in ``policy/outcome_routing.py``
    # so this preflight and the install-pipeline gate stay aligned.
    from .project_config import read_project_fetch_failure_default

    fetch_failure_default = read_project_fetch_failure_default(project_root)

    policy = route_discovery_outcome(
        fetch_result,
        logger=logger,
        fetch_failure_default=fetch_failure_default,
        raise_blocking_errors=not dry_run,
    )

    if policy is None:
        return fetch_result, False

    enforcement = policy.enforcement

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
                # #832: fall back to ``check.name`` when ``details`` is
                # empty so a failed check is never silently omitted from
                # the dry-run preview.
                items = check.details or [check.name]
                for detail in items:
                    dep_ref = _extract_dep_ref(detail, check.name)
                    if enforcement == "block":
                        block_lines.append((dep_ref, detail))
                    else:
                        warn_lines.append((dep_ref, detail))

            # Emit block bucket (capped)
            for dep_ref, detail in block_lines[:_DRY_RUN_PREVIEW_LIMIT]:
                logger.warning(f"Would be blocked by policy: {dep_ref} -- {detail}")
            overflow = len(block_lines) - _DRY_RUN_PREVIEW_LIMIT
            if overflow > 0:
                logger.warning(
                    f"... and {overflow} more would be blocked by policy. "
                    "Run `apm audit` for full report."
                )

            # Emit warn bucket (capped)
            for dep_ref, detail in warn_lines[:_DRY_RUN_PREVIEW_LIMIT]:
                logger.warning(f"Policy warning: {dep_ref} -- {detail}")
            overflow = len(warn_lines) - _DRY_RUN_PREVIEW_LIMIT
            if overflow > 0:
                logger.warning(
                    f"... and {overflow} more policy warnings. Run `apm audit` for full report."
                )
        else:
            # -- Real install: push each violation to DiagnosticCollector
            for check in audit_result.failed_checks:
                # Same fallback as dry-run: never silently drop a failed
                # check that happens to have empty ``details``.
                items = check.details or [check.name]
                for detail in items:
                    dep_ref = _extract_dep_ref(detail, check.name)
                    logger.policy_violation(
                        dep_ref=dep_ref,
                        reason=detail,
                        severity="block" if enforcement == "block" else "warn",
                        source=fetch_result.source,
                    )

        if enforcement == "block" and not dry_run:
            raise PolicyViolationError(
                f"Install blocked by org policy: {len(audit_result.failed_checks)} check(s) failed",
                audit_result=audit_result,
                policy_source=fetch_result.source,
            )

    return fetch_result, True
