"""Owner tests for policy discovery outcome routing (trust-policy-outcome).

Table-driven at ``apm_cli.policy.outcome_routing`` with a recording logger so
every outcome's log calls, fail-closed decision and user-facing block message
are pinned (see tests/mutation/floors.json).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.outcome_routing import resolve_trust_policy, route_discovery_outcome
from apm_cli.policy.schema import ApmPolicy

pytestmark = pytest.mark.unit

_FAIL_DEFAULT_OUTCOMES = (
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
    "no_git_remote",
    "absent",
    "empty",
)


class _Log:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def policy_resolved(self, **kwargs) -> None:
        self.calls.append(("policy_resolved", kwargs))

    def policy_discovery_miss(self, **kwargs) -> None:
        self.calls.append(("policy_discovery_miss", kwargs))


def _route(result: PolicyFetchResult, *, default: str = "warn", **kwargs):
    log = _Log()
    policy = route_discovery_outcome(result, logger=log, fetch_failure_default=default, **kwargs)
    return policy, log.calls


def _blocked(result: PolicyFetchResult, *, default: str = "warn", **kwargs):
    log = _Log()
    with pytest.raises(PolicyViolationError) as caught:
        route_discovery_outcome(result, logger=log, fetch_failure_default=default, **kwargs)
    return caught.value, log.calls


def test_disabled_and_unknown_outcomes_skip_without_logging() -> None:
    assert _route(PolicyFetchResult(outcome="disabled"), default="block") == (None, [])
    assert _route(PolicyFetchResult(outcome="surprise"), default="block") == (None, [])


@pytest.mark.parametrize("error_field", ["error", "fetch_error"])
def test_hash_mismatch_always_blocks_and_logs_error(error_field: str) -> None:
    result = PolicyFetchResult(outcome="hash_mismatch", source="org:a/.github")
    setattr(result, error_field, "pin differs")

    error, calls = _blocked(result, blocked_label="Install blocked")

    assert str(error) == (
        "Install blocked: policy hash mismatch -- pinned policy.hash does not match "
        "fetched policy bytes (source=org:a/.github). Update apm.yml policy.hash or "
        "contact your org admin."
    )
    assert error.policy_source == "org:a/.github"
    assert calls == [
        (
            "policy_discovery_miss",
            {"outcome": "hash_mismatch", "source": "org:a/.github", "error": "pin differs"},
        )
    ]


def test_hash_mismatch_without_source_names_unknown_and_respects_dry_run() -> None:
    result = PolicyFetchResult(outcome="hash_mismatch")

    error, _ = _blocked(result, blocked_label="X")
    assert "(source=unknown)" in str(error)
    assert error.policy_source == "unknown"

    assert _route(result, raise_blocking_errors=False)[0] is None


def test_incomplete_chain_always_blocks() -> None:
    result = PolicyFetchResult(outcome="incomplete_chain", source="org:a", error="parent 404")

    error, calls = _blocked(result, blocked_label="Blocked by org policy")

    assert str(error) == (
        "Blocked by org policy: org policy inheritance chain is incomplete "
        "(source=org:a). Restore the unreachable parent policy before retrying."
    )
    assert error.policy_source == "org:a"
    assert calls == [
        (
            "policy_discovery_miss",
            {"outcome": "incomplete_chain", "source": "org:a", "error": "parent 404"},
        )
    ]
    unsourced = PolicyFetchResult(outcome="incomplete_chain", fetch_error="boom")
    error, calls = _blocked(unsourced)
    assert error.policy_source == "unknown"
    assert "(source=unknown)" in str(error)
    assert calls[0][1]["error"] == "boom"
    assert _route(unsourced, raise_blocking_errors=False) == (
        None,
        [
            (
                "policy_discovery_miss",
                {"outcome": "incomplete_chain", "source": "", "error": "boom"},
            )
        ],
    )


@pytest.mark.parametrize("outcome", _FAIL_DEFAULT_OUTCOMES)
def test_no_policy_outcomes_warn_by_default(outcome: str) -> None:
    result = PolicyFetchResult(
        outcome=outcome, source="org:a", fetch_error="net", discovery_hint="Run x"
    )

    assert _route(result) == (
        None,
        [
            (
                "policy_discovery_miss",
                {"outcome": outcome, "source": "org:a", "error": "net", "hint": "Run x"},
            )
        ],
    )


@pytest.mark.parametrize("outcome", _FAIL_DEFAULT_OUTCOMES)
def test_no_policy_outcomes_block_when_project_opts_in(outcome: str) -> None:
    result = PolicyFetchResult(outcome=outcome, source="org:a", discovery_hint="Run x")

    error, _ = _blocked(result, default="block", blocked_label="Install blocked")

    assert str(error) == (
        f"Install blocked: no enforceable org policy was resolved (outcome={outcome}) "
        "and project apm.yml has policy.fetch_failure_default=block (source=org:a). Run x"
    )
    assert error.policy_source == "org:a"


def test_block_message_without_hint_or_source() -> None:
    error, _ = _blocked(PolicyFetchResult(outcome="absent"), default="block")

    assert str(error) == (
        "Install blocked: no enforceable org policy was resolved (outcome=absent) "
        "and project apm.yml has policy.fetch_failure_default=block (source=unknown)"
    )
    assert error.policy_source == "unknown"


def test_block_default_is_skipped_in_dry_run() -> None:
    result = PolicyFetchResult(outcome="malformed", error="bad yaml")

    policy, calls = _route(result, default="block", raise_blocking_errors=False)

    assert policy is None
    assert calls == [
        (
            "policy_discovery_miss",
            {"outcome": "malformed", "source": "", "error": "bad yaml", "hint": None},
        )
    ]


def _policy(fetch_failure: str = "warn") -> ApmPolicy:
    return ApmPolicy(enforcement="block", fetch_failure=fetch_failure)


def test_cached_stale_logs_resolved_then_miss_and_still_enforces() -> None:
    policy = _policy()
    result = PolicyFetchResult(
        outcome="cached_stale",
        policy=policy,
        source="org:a",
        fetch_error="timeout",
        cache_age_seconds=99,
        subgroup_scoped=True,
        chain_sources=["org:root", "org:a"],
    )

    returned, calls = _route(result, default="block")

    assert returned is policy
    assert calls == [
        (
            "policy_resolved",
            {
                "source": "org:a",
                "cached": True,
                "enforcement": "block",
                "age_seconds": 99,
                "subgroup_scoped": True,
                "chain": ["org:root", "org:a"],
            },
        ),
        (
            "policy_discovery_miss",
            {"outcome": "cached_stale", "source": "org:a", "error": "timeout"},
        ),
    ]


def test_cached_stale_blocks_when_cached_policy_declares_block() -> None:
    result = PolicyFetchResult(outcome="cached_stale", policy=_policy("block"), source="org:a")

    error, _ = _blocked(result, blocked_label="Install blocked")

    assert str(error) == (
        "Install blocked: org policy refresh failed and the cached policy declares "
        "fetch_failure=block (source=org:a)"
    )
    assert error.policy_source == "org:a"
    unsourced = PolicyFetchResult(outcome="cached_stale", policy=_policy("block"))
    error, _ = _blocked(unsourced)
    assert error.policy_source == "unknown"
    assert str(error).endswith("(source=unknown)")
    assert _route(unsourced, raise_blocking_errors=False)[0] is unsourced.policy


def test_cached_stale_without_policy_only_reports_the_miss() -> None:
    result = PolicyFetchResult(outcome="cached_stale", source="org:a")

    assert _route(result) == (
        None,
        [("policy_discovery_miss", {"outcome": "cached_stale", "source": "org:a", "error": None})],
    )


def test_found_announces_resolved_policy() -> None:
    policy = _policy()
    result = PolicyFetchResult(
        outcome="found",
        policy=policy,
        source="org:a",
        cached=True,
        cache_age_seconds=5,
        chain_sources=["org:a"],
    )

    returned, calls = _route(result, default="block")

    assert returned is policy
    assert calls == [
        (
            "policy_resolved",
            {
                "source": "org:a",
                "cached": True,
                "enforcement": "block",
                "age_seconds": 5,
                "subgroup_scoped": False,
                "chain": ["org:a"],
            },
        )
    ]
    assert _route(PolicyFetchResult(outcome="found")) == (None, [])


def test_routing_tolerates_missing_logger() -> None:
    policy = _policy()
    for outcome in ("found", "cached_stale"):
        result = PolicyFetchResult(outcome=outcome, policy=policy)
        assert route_discovery_outcome(result, logger=None, fetch_failure_default="warn") is policy
    for outcome in ("absent", "hash_mismatch", "incomplete_chain"):
        assert (
            route_discovery_outcome(
                PolicyFetchResult(outcome=outcome),
                logger=None,
                fetch_failure_default="warn",
                raise_blocking_errors=False,
            )
            is None
        )


def _project(tmp_path: Path, *, block: bool) -> Path:
    body = "name: p\nversion: 1.0.0\n"
    if block:
        body += "policy:\n  fetch_failure_default: block\n"
    (tmp_path / "apm.yml").write_text(body, encoding="utf-8")
    return tmp_path


def test_resolve_trust_policy_uses_project_default_and_neutral_label(tmp_path: Path) -> None:
    root = _project(tmp_path, block=True)

    with pytest.raises(PolicyViolationError) as caught:
        resolve_trust_policy(root, fetch_result=PolicyFetchResult(outcome="absent"))

    assert str(caught.value).startswith("Blocked by org policy: no enforceable org policy")
    assert (
        resolve_trust_policy(
            root,
            fetch_result=PolicyFetchResult(outcome="absent"),
            raise_blocking_errors=False,
        )
        is None
    )
    assert resolve_trust_policy(root, no_policy=True) is None


def test_resolve_trust_policy_reports_a_miss_once_and_never_announces_found(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, block=False)
    log = _Log()
    policy = _policy()

    returned = resolve_trust_policy(
        root, logger=log, fetch_result=PolicyFetchResult(outcome="found", policy=policy)
    )
    assert returned is policy
    assert log.calls == []

    resolve_trust_policy(root, logger=log, fetch_result=PolicyFetchResult(outcome="empty"))
    assert log.calls == [
        (
            "policy_discovery_miss",
            {"outcome": "empty", "source": "", "error": None, "hint": None},
        )
    ]

    silent = _Log()
    resolve_trust_policy(
        root,
        logger=silent,
        announce=False,
        fetch_result=PolicyFetchResult(outcome="empty"),
    )
    assert silent.calls == []
