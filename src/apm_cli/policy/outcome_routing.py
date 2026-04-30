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

from typing import TYPE_CHECKING, Optional  # noqa: F401

from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.schema import ApmPolicy


# Fetch-failure outcomes that honour the project-side
# ``policy.fetch_failure_default`` knob.  ``absent`` / ``no_git_remote``
# / ``empty`` are NOT failures -- they mean "no org policy" and are
# always fail-open.
_FETCH_FAILURE_OUTCOMES = (
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
)


_NON_FOUND_LOGGED_OUTCOMES = (
    "absent",
    "no_git_remote",
    "empty",
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
)


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
        outcomes in :data:`_FETCH_FAILURE_OUTCOMES`.
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

    # ``disabled`` is normally short-circuited by callers' escape
    # hatches; defensive fall-through here.
    if outcome == "disabled":
        return None

    # hash_mismatch (#827): ALWAYS fail closed.  A pin mismatch is an
    # explicit project-side trust assertion violation, not a transient
    # fetch failure -- the ``fetch_failure_default`` knob does not apply.
    if outcome == "hash_mismatch":
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
        return None

    # 6 of 9 non-found / non-disabled outcomes route through the
    # canonical logger helper for consistent wording (Logging C1/C2,
    # UX F1/F2/F4).
    if outcome in _NON_FOUND_LOGGED_OUTCOMES:
        if logger is not None:
            logger.policy_discovery_miss(
                outcome=outcome,
                source=source,
                error=fetch_result.error or fetch_result.fetch_error,
            )
        if (
            raise_blocking_errors
            and outcome in _FETCH_FAILURE_OUTCOMES
            and fetch_failure_default == "block"
        ):
            raise PolicyViolationError(
                "Install blocked: org policy could not be fetched / parsed "
                f"(outcome={outcome}) and project apm.yml has "
                "policy.fetch_failure_default=block "
                f"(source={source or 'unknown'})",
                policy_source=source or "unknown",
            )
        return None

    # cached_stale: warn but STILL enforce (caller proceeds with the
    # cached policy).  Order matches the pre-extraction policy_gate
    # behaviour: log policy_resolved first, then the discovery_miss
    # follow-up that explains the stale state.
    if outcome == "cached_stale":
        policy = fetch_result.policy
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

    # found: normal path
    if outcome == "found":
        policy = fetch_result.policy
        if logger is not None and policy is not None:
            logger.policy_resolved(
                source=source,
                cached=fetch_result.cached,
                enforcement=policy.enforcement,
                age_seconds=fetch_result.cache_age_seconds,
            )
        return policy

    # Defensive: unrecognised outcome -- skip enforcement.
    return None
