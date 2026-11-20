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

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.schema import ApmPolicy


# Genuine resolution failures with no policy usable by approval fallback.
POLICY_HASH_MISMATCH_OUTCOME = "hash_mismatch"
POLICY_RESOLUTION_FAILURE_OUTCOMES = frozenset(
    {
        "cache_miss_fetch_fail",
        "garbage_response",
        POLICY_HASH_MISMATCH_OUTCOME,
        "incomplete_chain",
        "malformed",
    }
)


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
    "incomplete_chain",
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

    if outcome == "incomplete_chain":
        if logger is not None:
            logger.policy_discovery_miss(
                outcome=outcome,
                source=source,
                error=fetch_result.error or fetch_result.fetch_error,
            )
        if raise_blocking_errors:
            raise PolicyViolationError(
                "Install blocked: org policy inheritance chain is incomplete "
                f"(source={source or 'unknown'}). Restore the unreachable parent "
                "policy before retrying.",
                policy_source=source or "unknown",
            )
        return None

    # 6 of 9 non-found / non-disabled outcomes route through the
    # canonical logger helper for consistent wording (Logging C1/C2,
    # UX F1/F2/F4).
    if outcome in _NON_FOUND_LOGGED_OUTCOMES:
        hint = fetch_result.discovery_hint
        if logger is not None:
            logger.policy_discovery_miss(
                outcome=outcome,
                source=source,
                error=fetch_result.error or fetch_result.fetch_error,
                hint=hint,
            )
        if (
            raise_blocking_errors
            and outcome in _OUTCOMES_HONORING_FETCH_FAILURE_DEFAULT
            and fetch_failure_default == "block"
        ):
            raise PolicyViolationError(
                "Install blocked: no enforceable org policy was resolved "
                f"(outcome={outcome}) and project apm.yml has "
                "policy.fetch_failure_default=block "
                f"(source={source or 'unknown'})" + (f". {hint}" if hint else ""),
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
                    subgroup_scoped=fetch_result.subgroup_scoped,
                    chain=fetch_result.chain_sources,
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
                subgroup_scoped=fetch_result.subgroup_scoped,
                chain=fetch_result.chain_sources,
            )
        return policy

    # Defensive: unrecognised outcome -- skip enforcement.
    return None


class _TrustReaderLog:
    """Logger adapter for trust readers.

    A trust reader runs after (or instead of) the command's own policy
    announcement, so a resolved policy is not announced again; a miss is
    always reported, through the caller's logger when it can render one.
    """

    def __init__(self, logger) -> None:
        self._logger = logger

    def policy_resolved(self, **_kwargs) -> None:
        return None

    def policy_discovery_miss(self, **kwargs) -> None:
        render = getattr(self._logger, "policy_discovery_miss", None)
        if render is None:
            from apm_cli.core.command_logger import InstallLogger

            verbose = bool(getattr(self._logger, "verbose", False))
            render = InstallLogger(verbose=verbose).policy_discovery_miss
        render(**kwargs)


def resolve_trust_policy(
    project_root: Path,
    *,
    no_policy: bool = False,
    logger=None,
    announce: bool = True,
    raise_blocking_errors: bool = True,
) -> ApmPolicy | None:
    """Return the org policy an executables-trust reader may apply.

    The single policy-outcome owner for trust readers. The discovery outcome
    routes through :func:`route_discovery_outcome` with the project's
    ``policy.fetch_failure_default``, exactly as package installs do: an
    unusable policy raises :class:`PolicyViolationError` when the project or
    the cached policy asks to block, and is reported otherwise.
    ``announce=False`` suppresses the
    miss report (for read-only replays whose command reports it);
    ``raise_blocking_errors=False`` serves dry-run previews.
    """
    if no_policy:
        return None
    from .discovery import PolicyFetchResult, discover_policy_with_chain
    from .project_config import read_project_fetch_failure_default

    root = Path(project_root)
    try:
        fetch_result = discover_policy_with_chain(root)
    except (OSError, ValueError, RuntimeError) as exc:
        fetch_result = PolicyFetchResult(outcome="cache_miss_fetch_fail", fetch_error=str(exc))
    return route_discovery_outcome(
        fetch_result,
        logger=_TrustReaderLog(logger) if announce else None,
        fetch_failure_default=read_project_fetch_failure_default(root),
        raise_blocking_errors=raise_blocking_errors,
    )
