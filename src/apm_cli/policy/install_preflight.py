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

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from pathlib import Path

# #832: Canonical exception type lives in ``apm_cli.install.errors``.
# ``PolicyBlockError`` remains as an alias re-exported below so external
# call sites that imported it from this module keep working.
from apm_cli.install.errors import PolicyViolationError

from .discovery import PolicyFetchResult, discover_policy_with_chain
from .outcome_routing import route_discovery_outcome
from .policy_checks import run_dependency_policy_checks

# Deprecated alias kept for backward compatibility (#832).  New code
# should ``raise``/``except`` :class:`PolicyViolationError` directly.
PolicyBlockError = PolicyViolationError


# Maximum lines to emit per severity bucket in dry-run preview.
# Overflow is collapsed into a single tail line pointing to ``apm audit``.
_DRY_RUN_PREVIEW_LIMIT = 5


def emit_check_violations(logger, check, *, severity: str, source: str | None) -> None:
    """Report one ``policy_violation`` per subject of *check*.

    Shared by the install pipeline gate and this preflight so both name the
    offending dependency or MCP server and pick the same remediation hint;
    the check name is only the category the hint cites. :meth:`CheckResult.violation_subjects` owns the
    check-to-subject projection.
    """
    for subject in check.violation_subjects():
        logger.policy_violation(
            dep_ref=subject.ref,
            reason=subject.reason,
            severity=severity,
            source=source,
            remedy=subject.remedy,
            declared_by=subject.declared_by,
            rule=check.name,
        )


def render_dry_run_verdicts(logger, audit_result, enforcement: str) -> None:
    """Render failed checks as a capped ``--dry-run`` preview per severity."""
    block_lines: list[tuple[str, str]] = []
    warn_lines: list[tuple[str, str]] = []
    for check in audit_result.failed_checks:
        # #832: a failed check without subjects still yields one entry
        # (keyed by its name), so it is never silently omitted.
        for subject in check.violation_subjects():
            line = (subject.ref, subject.reason)
            if enforcement == "block":
                block_lines.append(line)
            else:
                warn_lines.append(line)

    for dep_ref, detail in block_lines[:_DRY_RUN_PREVIEW_LIMIT]:
        logger.warning(f"Would be blocked by policy: {dep_ref} -- {detail}")
    overflow = len(block_lines) - _DRY_RUN_PREVIEW_LIMIT
    if overflow > 0:
        logger.warning(
            f"... and {overflow} more would be blocked by policy. Run `apm audit` for full report."
        )

    for dep_ref, detail in warn_lines[:_DRY_RUN_PREVIEW_LIMIT]:
        logger.warning(f"Policy warning: {dep_ref} -- {detail}")
    overflow = len(warn_lines) - _DRY_RUN_PREVIEW_LIMIT
    if overflow > 0:
        logger.warning(f"... and {overflow} more policy warnings. Run `apm audit` for full report.")


def run_policy_preflight(
    *,
    project_root: Path,
    apm_deps=None,
    mcp_deps=None,
    mcp_brought_in_by: Mapping[str, str] | None = None,
    no_policy: bool = False,
    logger,
    dry_run: bool = False,
    registries: dict[str, str] | None = None,
    effective_target: tuple[str, ...] | None = None,
    effective_target_source: str = "effective target",
    fetch_result: PolicyFetchResult | None = None,
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
    mcp_brought_in_by:
        Transitive MCP server name -> direct apm.yml dependency key that
        brings it in (``CurrentMcpConfigView.brought_in_by``), so removal
        hints name an entry the user can remove.
    effective_target:
        ``EffectiveTargetDecision.canonical_targets`` of the resolved target
        selection, for compilation-target policy.
    effective_target_source:
        Where that selection came from, named in violation details.
    fetch_result:
        A discovery the caller already holds, so one install discovers once.
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
    from .discovery import policy_disabled_by_env

    if no_policy or policy_disabled_by_env():
        reason = "--no-policy" if no_policy else "APM_POLICY_DISABLE=1"
        logger.policy_disabled(reason)
        return None, False

    # -- Discovery (chain-aware: resolves extends: + merges) -----------
    if fetch_result is None:
        fetch_result = discover_policy_with_chain(project_root)

    # -- Route the outcome through the policy-outcome routing owner ---
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
    # ``apm_deps`` here is always the direct-deps list from the caller
    # (manifest or MCP path) -- forward as direct_dep_keys so the
    # require_pinned_constraint check skips transitives (#1494 Copilot review).
    apm_deps_list = list(apm_deps) if apm_deps is not None else []
    audit_result = run_dependency_policy_checks(
        apm_deps_list,
        lockfile=None,
        policy=policy,
        mcp_deps=mcp_deps,
        mcp_brought_in_by=mcp_brought_in_by,
        effective_target=effective_target,
        effective_target_source=effective_target_source,
        fail_fast=(enforcement == "block"),
        registries=registries,
        direct_dep_keys={d.get_unique_key() for d in apm_deps_list},
    )

    if not audit_result.passed:
        if dry_run:
            render_dry_run_verdicts(logger, audit_result, enforcement)
        else:
            # -- Real install: push each violation to DiagnosticCollector
            for check in audit_result.failed_checks:
                emit_check_violations(
                    logger,
                    check,
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


def prefetch_identity_policy(policy_root: Path, *, no_policy: bool = False):
    """The blocking org policy that refuses a dependency before it is fetched.

    A deny or allow rule decides on the dependency identity alone, so a
    dependency the policy blocks is never contacted, cloned or cached.
    Returns ``None`` when policy is disabled, unusable or not blocking;
    the install gate then reports the outcome as before.
    """
    from .discovery import policy_disabled_by_env
    from .outcome_routing import resolve_trust_policy

    if no_policy or policy_disabled_by_env():
        return None
    policy = resolve_trust_policy(policy_root, announce=False, raise_blocking_errors=False)
    if policy is None or policy.enforcement != "block":
        return None
    return policy


def identity_policy_denial(dependency, policy) -> str | None:
    """The reason *policy* refuses *dependency* by identity, else ``None``."""
    if policy is None:
        return None
    from .matcher import check_dependency_allowed

    allowed, reason = check_dependency_allowed(dependency, policy.dependencies)
    return None if allowed else reason


_IDENTITY_POLICY: ContextVar[object | None] = ContextVar("identity_policy", default=None)


@contextlib.contextmanager
def identity_policy_scope(policy_root: Path, *, no_policy: bool = False) -> Iterator[None]:
    """Make the blocking policy visible to package validation for one install."""
    policy = prefetch_identity_policy(Path(policy_root).resolve(), no_policy=no_policy)
    token = _IDENTITY_POLICY.set(policy)
    try:
        yield
    finally:
        _IDENTITY_POLICY.reset(token)


def active_identity_policy():
    """The blocking policy of the enclosing :func:`identity_policy_scope`, if any."""
    return _IDENTITY_POLICY.get()


def identity_policy_refuses(dependency) -> bool:
    """Whether the enclosing scope's policy refuses *dependency*, so it is never probed.

    The install's policy gate reports the refusal.
    """
    return identity_policy_denial(dependency, active_identity_policy()) is not None
