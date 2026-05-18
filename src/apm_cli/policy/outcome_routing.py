"""Single source of truth for the 9-outcome policy-discovery routing table.

Both the install pipeline gate (``install/phases/policy_gate.py``) and
the non-pipeline preflight helper (``policy/install_preflight.py``) need
to translate a :class:`~apm_cli.policy.discovery.PolicyFetchResult` into
the same set of side-effects:

* emit the correct ``logger.policy_discovery_miss`` /
  ``logger.policy_resolved`` line for the outcome, and
* decide whether to fail closed -- raising
  :class:`~apm_cli.install.errors.PolicyViolationError` -- based on the
  project's ``policy.fetch_failure_default`` and the cached policy's
  own ``fetch_failure`` knob.

Before #832 those decisions were duplicated across the two files.  This
module is the extracted shared core; the two callers now only own the
logic that is genuinely different (how they react after routing -- e.g.
the dry-run preview path in ``install_preflight``, or the post-routing
enforcement gate in ``policy_gate``).

This is a refactor: the wording, the order of log calls per branch,
and the exact gating semantics match the pre-extraction behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.schema import ApmPolicy


# Outcomes that honour the project-side ``policy.fetch_failure_default``
# knob.  Despite the historical name "fetch failure", this set ALSO
# includes the no-policy outcomes ``no_git_remote`` / ``absent`` /
# ``empty`` -- pre-#1159 those were excluded and were always fail-open
# even when the project explicitly opted in to ``block``.  That was an
# install-path silent-skip (governance bypass) symmetrical to the audit
# bug fixed in the same PR.  Membership rule: an outcome belongs here
# iff a project that asserts ``policy.fetch_failure_default: block``
# expects "no enforceable policy" to fail closed for that outcome on
# BOTH install and audit paths.
_OUTCOMES_HONORING_FETCH_FAILURE_DEFAULT = (
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
    "no_git_remote",
    "absent",
    "empty",
)


_NON_FOUND_LOGGED_OUTCOMES = (
    "absent",
    "no_git_remote",
    "empty",
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
)


@dataclass(frozen=True, slots=True)
class NonFoundOpts:
    """Options for _handle_non_found_outcome helper."""

    outcome: str
    source: str | None
    fetch_result: object
    logger: object
    fetch_failure_default: str
    raise_blocking_errors: bool


def route_discovery_outcome(
    fetch_result: PolicyFetchResult,
    *,
    logger,
    fetch_failure_default: str,
    raise_blocking_errors: bool = True,
) -> ApmPolicy | None:
    """Route a :class:`PolicyFetchResult` to logging + fail-closed decisions.

    Parameters
    ----------
    fetch_result:
        Result of ``discover_policy_with_chain``.
    logger:
        An :class:`~apm_cli.core.command_logger.InstallLogger` (or any
        object exposing ``policy_resolved`` / ``policy_discovery_miss``).
        ``None`` is tolerated for non-CLI callers.
    fetch_failure_default:
        Project-side ``policy.fetch_failure_default``; one of
        ``"warn"`` (default) or ``"block"``.  Only consulted for
        outcomes in :data:`_OUTCOMES_HONORING_FETCH_FAILURE_DEFAULT`.
    raise_blocking_errors:
        When ``True`` (default), raise :class:`PolicyViolationError` for
        outcomes that demand fail-closed behaviour (hash mismatch,
        fetch failure under ``block``, cached_stale with
        ``policy.fetch_failure=block``).  When ``False`` (used by
        ``install --dry-run``), the function returns normally and the
        caller is expected to render a preview instead.

    Returns
    -------
    Optional[ApmPolicy]
        The merged effective policy when the caller should proceed to
        per-dependency enforcement; ``None`` when the caller should
        skip enforcement (no policy resolved, or fail-open).
    """
    outcome = fetch_result.outcome
    source = fetch_result.source

    # Early returns for simple outcomes
    if outcome == "disabled":
        return None

    if outcome == "hash_mismatch":
        return _handle_hash_mismatch(fetch_result, logger, raise_blocking_errors)

    if outcome in _NON_FOUND_LOGGED_OUTCOMES:
        return _handle_non_found_outcome(
            NonFoundOpts(
                outcome=outcome,
                source=source,
                fetch_result=fetch_result,
                logger=logger,
                fetch_failure_default=fetch_failure_default,
                raise_blocking_errors=raise_blocking_errors,
            )
        )

    if outcome == "cached_stale":
        return _handle_cached_stale(fetch_result, logger, raise_blocking_errors)

    if outcome == "found":
        return _handle_found(fetch_result, logger)

    # Defensive: unrecognised outcome -- skip enforcement.
    return None


def _handle_hash_mismatch(
    fetch_result: PolicyFetchResult,
    logger,
    raise_blocking_errors: bool,
) -> None:
    """Handle hash_mismatch outcome."""
    source = fetch_result.source
    if logger is not None:
        logger.policy_discovery_miss(
            outcome="hash_mismatch",
            source=source,
            error=fetch_result.error or fetch_result.fetch_error,
        )
    if raise_blocking_errors:
        raise PolicyViolationError(
            "Install blocked: policy hash mismatch -- pinned policy.hash "
            "does not match fetched policy bytes "
            f"(source={source or 'unknown'}). "
            "Update apm.yml policy.hash or contact your org admin.",
            policy_source=source or "unknown",
        )


def _handle_non_found_outcome(opts: NonFoundOpts) -> None:
    """Handle non-found logged outcomes."""
    if opts.logger is not None:
        opts.logger.policy_discovery_miss(
            outcome=opts.outcome,
            source=opts.source,
            error=opts.fetch_result.error or opts.fetch_result.fetch_error,
        )
    if (
        opts.raise_blocking_errors
        and opts.outcome in _OUTCOMES_HONORING_FETCH_FAILURE_DEFAULT
        and opts.fetch_failure_default == "block"
    ):
        raise PolicyViolationError(
            "Install blocked: no enforceable org policy was resolved "
            f"(outcome={opts.outcome}) and project apm.yml has "
            "policy.fetch_failure_default=block "
            f"(source={opts.source or 'unknown'})",
            policy_source=opts.source or "unknown",
        )


def _handle_cached_stale(
    fetch_result: PolicyFetchResult,
    logger,
    raise_blocking_errors: bool,
) -> ApmPolicy | None:
    """Handle cached_stale outcome."""
    policy = fetch_result.policy
    source = fetch_result.source
    if logger is not None:
        if policy is not None:
            logger.policy_resolved(
                source=source,
                cached=True,
                enforcement=policy.enforcement,
                age_seconds=fetch_result.cache_age_seconds,
            )
        logger.policy_discovery_miss(
            outcome="cached_stale",
            source=source,
            error=fetch_result.fetch_error,
        )
    if raise_blocking_errors and policy is not None and policy.fetch_failure == "block":
        raise PolicyViolationError(
            "Install blocked: org policy refresh failed and the cached "
            "policy declares fetch_failure=block "
            f"(source={source or 'unknown'})",
            policy_source=source or "unknown",
        )
    return policy


def _handle_found(fetch_result: PolicyFetchResult, logger) -> ApmPolicy | None:
    """Handle found outcome."""
    policy = fetch_result.policy
    source = fetch_result.source
    if logger is not None and policy is not None:
        logger.policy_resolved(
            source=source,
            cached=fetch_result.cached,
            enforcement=policy.enforcement,
            age_seconds=fetch_result.cache_age_seconds,
        )
    return policy
