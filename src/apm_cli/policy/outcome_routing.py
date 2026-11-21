"""The policy-outcome routing owner: one decision per policy-discovery outcome.

Every command that reads org policy -- the install gate
(``install/phases/policy_gate.py``), the preflight helper
(``policy/install_preflight.py``), ``apm audit --ci``, ``apm approve``,
``apm update`` and the executables-trust readers -- translates a
:class:`~apm_cli.policy.discovery.PolicyFetchResult` through this module:

* :data:`POLICY_DISCOVERY_OUTCOMES` is the closed set of 11 outcomes and
  ``_OUTCOME_ROUTES`` gives each exactly one route; an outcome outside the
  set fails closed.
* :func:`classify_discovery_outcome` is the pure decision (policy to
  enforce, block reason, whether org policy was unresolved), based on the
  project's ``policy.fetch_failure_default`` and the cached policy's own
  ``fetch_failure`` knob.
* :func:`route_discovery_outcome` adds the ``logger.policy_resolved`` /
  ``logger.policy_discovery_miss`` lines and raises
  :class:`~apm_cli.install.errors.PolicyViolationError` on a block.
* :func:`resolve_trust_policy` serves readers that only need the policy.

Callers own only how they render the decision (for example the dry-run
preview in ``install_preflight`` or the ``[x]`` / ``[!]`` lines of
``apm audit --ci``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.remediation import POLICY_FETCH_FAILURE_BLOCKED
from apm_cli.install.errors import PolicyViolationError

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.schema import ApmPolicy


# The closed set of policy-discovery outcomes. ``PolicyFetchResult.outcome``
# takes exactly one of these values; every one has a route below, and an
# outcome outside the set fails closed.
POLICY_DISCOVERY_OUTCOMES = frozenset(
    {
        "found",
        "absent",
        "cached_stale",
        "cache_miss_fetch_fail",
        "malformed",
        "disabled",
        "garbage_response",
        "no_git_remote",
        "empty",
        "hash_mismatch",
        "incomplete_chain",
        "ambiguous_git_remote",
    }
)


# Genuine resolution failures with no policy usable by approval fallback.
POLICY_HASH_MISMATCH_OUTCOME = "hash_mismatch"
# Several git remotes and no ``origin``: discovery fails closed (req-pl-012).
AMBIGUOUS_GIT_REMOTE_OUTCOME = "ambiguous_git_remote"
POLICY_RESOLUTION_FAILURE_OUTCOMES = frozenset(
    {
        "cache_miss_fetch_fail",
        "garbage_response",
        POLICY_HASH_MISMATCH_OUTCOME,
        "incomplete_chain",
        "malformed",
    }
)


# How each outcome routes.
_ENFORCE = "enforce"  # found: enforce the policy
_ENFORCE_CACHED = "enforce-cached"  # cached_stale: enforce, block on fetch_failure=block
_DISABLED = "disabled"  # disabled: no policy, nothing to report
_FETCH_FAILURE_DEFAULT = "fetch-failure-default"  # no usable policy: honour the knob
_ALWAYS_BLOCK = "always-block"  # a trust assertion failed: never fall open

# Membership in _FETCH_FAILURE_DEFAULT: an outcome routes there iff a
# project that asserts ``policy.fetch_failure_default: block`` expects "no
# enforceable policy" to fail closed for that outcome on every command
# (#1159: the no-policy outcomes ``no_git_remote`` / ``absent`` / ``empty``
# included).
_OUTCOME_ROUTES: dict[str, str] = {
    "found": _ENFORCE,
    "cached_stale": _ENFORCE_CACHED,
    "disabled": _DISABLED,
    "absent": _FETCH_FAILURE_DEFAULT,
    "no_git_remote": _FETCH_FAILURE_DEFAULT,
    "empty": _FETCH_FAILURE_DEFAULT,
    "malformed": _FETCH_FAILURE_DEFAULT,
    "cache_miss_fetch_fail": _FETCH_FAILURE_DEFAULT,
    "garbage_response": _FETCH_FAILURE_DEFAULT,
    "hash_mismatch": _ALWAYS_BLOCK,
    "incomplete_chain": _ALWAYS_BLOCK,
    "ambiguous_git_remote": _ALWAYS_BLOCK,
}

if set(_OUTCOME_ROUTES) != POLICY_DISCOVERY_OUTCOMES:  # pragma: no cover - import-time guard
    raise RuntimeError("every policy-discovery outcome needs exactly one route")


@dataclass(frozen=True)
class DiscoveryRoute:
    """The routed decision for one :class:`PolicyFetchResult`.

    ``policy`` is the policy to enforce (``None``: no policy applies).
    ``block_reason`` is set when the outcome fails closed. ``unresolved`` is
    ``True`` when org policy exists or was asserted but could not be read,
    so a trust decision made without it is not final.
    ``from_fetch_failure_default`` is ``True`` when the block comes from the
    project's ``policy.fetch_failure_default: block``.
    """

    outcome: str
    kind: str
    policy: ApmPolicy | None
    block_reason: str | None = None
    unresolved: bool = False
    from_fetch_failure_default: bool = False

    @property
    def disabled(self) -> bool:
        return self.kind == _DISABLED

    @property
    def report_miss(self) -> bool:
        """Whether the outcome warrants a policy-miss report."""
        return self.kind not in (_ENFORCE, _DISABLED)

    @property
    def blocks(self) -> bool:
        return self.block_reason is not None


def classify_discovery_outcome(
    fetch_result: PolicyFetchResult, *, fetch_failure_default: str
) -> DiscoveryRoute:
    """Decide how *fetch_result* routes; the one decision every policy reader uses.

    Pure: no logging, no raising. An outcome outside
    :data:`POLICY_DISCOVERY_OUTCOMES` blocks.
    """
    outcome = getattr(fetch_result, "outcome", "")
    source = getattr(fetch_result, "source", "") or "unknown"
    policy = getattr(fetch_result, "policy", None)
    route = _OUTCOME_ROUTES.get(outcome)
    unresolved = outcome in POLICY_RESOLUTION_FAILURE_OUTCOMES

    if route == _ENFORCE:
        return DiscoveryRoute(outcome, route, policy)
    if route == _DISABLED:
        return DiscoveryRoute(outcome, route, None)
    if route == _ENFORCE_CACHED:
        reason = None
        if policy is not None and policy.fetch_failure == "block":
            reason = (
                "org policy refresh failed and the cached policy declares "
                f"fetch_failure=block (source={source})"
            )
        return DiscoveryRoute(outcome, route, policy, block_reason=reason)
    if route == _FETCH_FAILURE_DEFAULT:
        reason = None
        if fetch_failure_default == "block":
            hint = getattr(fetch_result, "discovery_hint", None)
            err_text = (
                getattr(fetch_result, "error", None)
                or getattr(fetch_result, "fetch_error", None)
                or outcome
            )
            cause = outcome_cause(outcome, source, err_text)
            sentences = [
                f"no enforceable org policy was resolved (outcome={outcome}) and "
                "project apm.yml has policy.fetch_failure_default=block "
                f"(source={source})",
                cause.rstrip("."),
            ]
            if hint:
                sentences.append(hint.rstrip("."))
            named = f" ({source})" if source != "unknown" else ""
            remedy = POLICY_FETCH_FAILURE_BLOCKED.bind(source=named).sentence()
            reason = ". ".join([*sentences, remedy])
        return DiscoveryRoute(
            outcome,
            route,
            None,
            block_reason=reason,
            unresolved=unresolved,
            from_fetch_failure_default=reason is not None,
        )
    if outcome == POLICY_HASH_MISMATCH_OUTCOME:
        # #827: a pin mismatch is a project-side trust assertion violation,
        # not a transient fetch failure; fetch_failure_default does not apply.
        reason = (
            "policy hash mismatch -- pinned policy.hash does not match fetched "
            f"policy bytes (source={source}). Update apm.yml policy.hash or "
            "contact your org admin."
        )
    elif outcome == "incomplete_chain":
        reason = (
            f"org policy inheritance chain is incomplete (source={source}). "
            "Restore the unreachable parent policy before retrying."
        )
    elif outcome == AMBIGUOUS_GIT_REMOTE_OUTCOME:
        reason = getattr(fetch_result, "error", None) or "several git remotes and no 'origin'"
    else:
        reason = f"unrecognized policy discovery outcome {outcome!r} (source={source})"
    return DiscoveryRoute(outcome, _ALWAYS_BLOCK, None, block_reason=reason, unresolved=True)


def outcome_cause(outcome: str, source: str | None, err_text: str | None) -> str:
    """Why discovery resolved no usable policy, in the words every reader prints.

    ``audit --ci`` prints it on its own line; a block under
    ``fetch_failure_default=block`` appends it to the routed reason, so a
    refusal always says why the org policy did not resolve (#1159).
    """
    src = source or "unknown"
    if outcome == "no_git_remote":
        return "Could not determine org from git remote"
    if outcome == "absent":
        return f"No org policy found at {src}"
    if outcome == "empty":
        return f"Org policy at {src} is present but empty"
    # malformed / cache_miss_fetch_fail / garbage_response (and any
    # `error` set on the result): preserve the legacy wording so existing
    # consumers parsing the line keep working.
    return f"Policy fetch failed: {err_text or outcome}"


def route_discovery_outcome(
    fetch_result: PolicyFetchResult,
    *,
    logger,
    fetch_failure_default: str,
    raise_blocking_errors: bool = True,
    blocked_label: str = "Install blocked",
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
        outcomes that carry no usable policy.
    raise_blocking_errors:
        When ``True`` (default), raise :class:`PolicyViolationError` for
        outcomes that demand fail-closed behaviour (see
        :func:`classify_discovery_outcome`).  When ``False`` (used by
        ``install --dry-run``), the function returns normally and the
        caller is expected to render a preview instead.
    blocked_label:
        Prefix of a :class:`PolicyViolationError` message; readers outside
        ``apm install`` name the block neutrally.

    Returns
    -------
    Optional[ApmPolicy]
        The merged effective policy when the caller should proceed to
        per-dependency enforcement; ``None`` when the caller should
        skip enforcement (no policy resolved, or fail-open).
    """
    route = classify_discovery_outcome(fetch_result, fetch_failure_default=fetch_failure_default)
    source = fetch_result.source
    policy = route.policy
    if logger is not None:
        # cached_stale logs policy_resolved first, then the miss that
        # explains the stale state.
        stale = route.kind == _ENFORCE_CACHED
        if policy is not None:
            logger.policy_resolved(
                source=source,
                cached=fetch_result.cached or stale,
                enforcement=policy.enforcement,
                age_seconds=fetch_result.cache_age_seconds,
                subgroup_scoped=fetch_result.subgroup_scoped,
                chain=fetch_result.chain_sources,
            )
        # A fetch_failure_default block raises, and the exception (which
        # carries any hint) is its only report: the miss wording ("proceeding
        # without policy enforcement") would contradict it (#4180).
        if route.report_miss and not (route.from_fetch_failure_default and raise_blocking_errors):
            miss: dict = {
                "outcome": route.outcome,
                "source": source,
                "error": fetch_result.error or fetch_result.fetch_error,
            }
            if stale:
                miss["error"] = fetch_result.fetch_error
            elif route.kind == _FETCH_FAILURE_DEFAULT:
                miss["hint"] = fetch_result.discovery_hint
            logger.policy_discovery_miss(**miss)
    if route.blocks and raise_blocking_errors:
        raise PolicyViolationError(
            f"{blocked_label}: {route.block_reason}",
            policy_source=source or "unknown",
        )
    if route.blocks and logger is not None:
        # A dry run previews the block the real command applies (#4455).
        logger.warning(f"Would be blocked by policy: {route.block_reason}")
    return policy


class _TrustReaderLog:
    """Logger adapter for trust readers.

    A trust reader runs after (or instead of) the command's own policy
    announcement, so a resolved policy is not announced again. A miss is
    reported once the outcome is known not to block: a blocking outcome
    raises, and the exception is then the only report.
    """

    def __init__(self, logger) -> None:
        self._logger = logger
        self._pending: list[dict] = []

    def policy_resolved(self, **_kwargs) -> None:
        return None

    def policy_discovery_miss(self, **kwargs) -> None:
        self._pending.append(kwargs)

    def flush(self) -> None:
        render = getattr(self._logger, "policy_discovery_miss", None)
        for kwargs in self._pending:
            if render is not None:
                render(**kwargs)
            else:
                _report_miss_neutrally(self._logger, **kwargs)
        self._pending.clear()


def _report_miss_neutrally(logger, *, outcome: str, source: str = "", error=None, **_kw) -> None:
    """Report a policy miss for a command that has no policy wording of its own."""
    verbose = bool(getattr(logger, "verbose", False))
    if outcome in ("absent", "no_git_remote") and not verbose:
        return
    from apm_cli.utils.console import _rich_warning

    detail = f": {error}" if error else ""
    if outcome == "cached_stale":
        message = (
            f"Using stale cached org policy (refresh failed{detail}); enforcement still applies."
        )
    else:
        message = (
            f"Org policy not applied (outcome={outcome}, source={source or 'unknown'}){detail}"
        )
    _rich_warning(message, symbol="warning")


def discover_trust_policy(project_root: Path) -> PolicyFetchResult:
    """Discover org policy for *project_root*; a discovery error is a fetch failure.

    A command that has several trust readers discovers once and hands the
    result to each of them (a local bundle install, for example).
    """
    from .discovery import PolicyFetchResult, discover_policy_with_chain

    try:
        return discover_policy_with_chain(Path(project_root))
    except (OSError, ValueError, RuntimeError) as exc:
        # The exception text can carry transport detail; report its type only.
        return PolicyFetchResult(
            outcome="cache_miss_fetch_fail",
            fetch_error=f"policy discovery failed ({type(exc).__name__})",
        )


def classify_project_policy(
    project_root: Path, *, fetch_result: PolicyFetchResult | None = None
) -> DiscoveryRoute:
    """Discover org policy for *project_root* and return its routed decision.

    For readers that act on the decision themselves instead of raising
    (``apm update`` reconcile, MCP admission).
    """
    from .project_config import read_project_fetch_failure_default

    root = Path(project_root)
    result = fetch_result if fetch_result is not None else discover_trust_policy(root)
    return classify_discovery_outcome(
        result, fetch_failure_default=read_project_fetch_failure_default(root)
    )


def resolve_trust_policy(
    project_root: Path,
    *,
    no_policy: bool = False,
    logger=None,
    announce: bool = True,
    raise_blocking_errors: bool = True,
    fetch_result: PolicyFetchResult | None = None,
) -> ApmPolicy | None:
    """Return the org policy an executables-trust reader may apply.

    The single policy-outcome owner for trust readers. The discovery outcome
    routes through :func:`route_discovery_outcome` with the project's
    ``policy.fetch_failure_default``, exactly as package installs do: an
    unusable policy raises :class:`PolicyViolationError` when the project or
    the cached policy asks to block, and is reported otherwise.
    ``announce=False`` suppresses the
    miss report (for read-only replays whose command reports it);
    ``raise_blocking_errors=False`` serves dry-run previews. *fetch_result*
    is a discovery the caller already holds, from :func:`discover_trust_policy`.
    """
    if no_policy:
        return None
    from .project_config import read_project_fetch_failure_default

    root = Path(project_root)
    if fetch_result is None:
        fetch_result = discover_trust_policy(root)
    log = _TrustReaderLog(logger) if announce else None
    policy = route_discovery_outcome(
        fetch_result,
        logger=log,
        fetch_failure_default=read_project_fetch_failure_default(root),
        raise_blocking_errors=raise_blocking_errors,
        blocked_label="Blocked by org policy",
    )
    if log is not None:
        log.flush()
    return policy
