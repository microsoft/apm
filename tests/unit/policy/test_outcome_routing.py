"""policy-outcome-routing owner: one decision per policy-discovery outcome (#4121)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy import PolicyFetchResult, outcome_routing
from apm_cli.policy.outcome_routing import (
    classify_discovery_outcome,
    classify_project_policy,
    discover_trust_policy,
    outcome_cause,
    resolve_trust_policy,
    route_discovery_outcome,
)
from apm_cli.policy.schema import ApmPolicy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-policy-outcome-routing")]

# Collaborators replaced at their own module: discovery (network, git, cache) and console.
DISCOVERY = "apm_cli.policy.discovery.discover_policy_with_chain"
WARNING = "apm_cli.utils.console._rich_warning"

SRC = "org:contoso/.github"
FETCH_FAILURE_DEFAULT_OUTCOMES = (
    "absent",
    "no_git_remote",
    "empty",
    "malformed",
    "cache_miss_fetch_fail",
    "garbage_response",
)
RESOLUTION_FAILURES = {"cache_miss_fetch_fail", "garbage_response", "malformed"}
REMEDY = "Run 'apm policy status' to check that the org policy source{} is reachable, then retry, or ask your org admin."


def _result(outcome: str, **fields: object) -> PolicyFetchResult:
    return PolicyFetchResult(outcome=outcome, source=fields.pop("source", SRC), **fields)


# -- classify_discovery_outcome ----------------------------------------------


def test_found_enforces_the_policy() -> None:
    policy = ApmPolicy(enforcement="block")
    route = classify_discovery_outcome(
        _result("found", policy=policy), fetch_failure_default="block"
    )
    assert (route.kind, route.policy, route.block_reason, route.unresolved) == (
        "enforce",
        policy,
        None,
        False,
    )
    assert (route.blocks, route.report_miss, route.disabled) == (False, False, False)


def test_disabled_applies_no_policy_and_reports_nothing() -> None:
    route = classify_discovery_outcome(
        _result("disabled", policy=ApmPolicy()), fetch_failure_default="block"
    )
    assert (route.kind, route.policy, route.blocks) == ("disabled", None, False)
    assert (route.disabled, route.report_miss, route.unresolved) == (True, False, False)


@pytest.mark.parametrize(
    ("knob", "reason"),
    [
        ("warn", None),
        (
            "block",
            "org policy refresh failed and the cached policy declares fetch_failure=block "
            f"(source={SRC})",
        ),
    ],
)
def test_cached_stale_enforces_and_blocks_only_on_its_own_knob(
    knob: str, reason: str | None
) -> None:
    policy = ApmPolicy(fetch_failure=knob)
    route = classify_discovery_outcome(
        _result("cached_stale", policy=policy), fetch_failure_default="warn"
    )
    assert (route.kind, route.policy, route.block_reason) == ("enforce-cached", policy, reason)
    assert (route.report_miss, route.unresolved, route.from_fetch_failure_default) == (
        True,
        False,
        False,
    )


def test_cached_stale_without_a_policy_never_blocks() -> None:
    route = classify_discovery_outcome(_result("cached_stale"), fetch_failure_default="block")
    assert (route.policy, route.block_reason) == (None, None)


@pytest.mark.parametrize("outcome", FETCH_FAILURE_DEFAULT_OUTCOMES)
def test_no_usable_policy_warns_by_default(outcome: str) -> None:
    route = classify_discovery_outcome(
        _result(outcome, policy=ApmPolicy()), fetch_failure_default="warn"
    )
    assert (route.kind, route.policy, route.block_reason) == ("fetch-failure-default", None, None)
    assert route.unresolved is (outcome in RESOLUTION_FAILURES)
    assert (route.from_fetch_failure_default, route.report_miss) == (False, True)


@pytest.mark.parametrize("outcome", FETCH_FAILURE_DEFAULT_OUTCOMES)
def test_no_usable_policy_blocks_when_the_project_asks(outcome: str) -> None:
    route = classify_discovery_outcome(_result(outcome), fetch_failure_default="block")
    assert route.block_reason is not None and route.blocks
    assert route.block_reason.startswith(
        f"no enforceable org policy was resolved (outcome={outcome}) and project apm.yml has "
        f"policy.fetch_failure_default=block (source={SRC}). "
    )
    assert route.from_fetch_failure_default is True
    assert route.unresolved is (outcome in RESOLUTION_FAILURES)


def test_fetch_failure_block_reason_lists_cause_hint_and_remedy() -> None:
    route = classify_discovery_outcome(
        _result("absent", discovery_hint="The repo may be private."),
        fetch_failure_default="block",
    )
    assert route.block_reason == (
        "no enforceable org policy was resolved (outcome=absent) and project apm.yml has "
        f"policy.fetch_failure_default=block (source={SRC}). No org policy found at {SRC}. "
        f"The repo may be private. {REMEDY.format(f' ({SRC})')}"
    )


def test_fetch_failure_block_reason_without_a_source_names_none() -> None:
    route = classify_discovery_outcome(_result("empty", source=""), fetch_failure_default="block")
    assert route.block_reason == (
        "no enforceable org policy was resolved (outcome=empty) and project apm.yml has "
        "policy.fetch_failure_default=block (source=unknown). "
        f"Org policy at unknown is present but empty. {REMEDY.format('')}"
    )


@pytest.mark.parametrize(
    ("fields", "cause"),
    [
        ({"error": "e1", "fetch_error": "e2"}, "Policy fetch failed: e1"),
        ({"fetch_error": "e2"}, "Policy fetch failed: e2"),
        ({}, "Policy fetch failed: malformed"),
    ],
)
def test_fetch_failure_cause_prefers_error_then_fetch_error(fields: dict, cause: str) -> None:
    route = classify_discovery_outcome(
        _result("malformed", **fields), fetch_failure_default="block"
    )
    assert f". {cause}. Run 'apm policy status'" in str(route.block_reason)


@pytest.mark.parametrize(
    ("outcome", "fields", "reason"),
    [
        (
            "hash_mismatch",
            {},
            "policy hash mismatch -- pinned policy.hash does not match fetched policy bytes "
            f"(source={SRC}). Update apm.yml policy.hash or contact your org admin.",
        ),
        (
            "incomplete_chain",
            {},
            f"org policy inheritance chain is incomplete (source={SRC}). "
            "Restore the unreachable parent policy before retrying.",
        ),
        ("ambiguous_git_remote", {"error": "remotes a, b"}, "remotes a, b"),
        ("ambiguous_git_remote", {}, "several git remotes and no 'origin'"),
        ("surprise", {}, f"unrecognized policy discovery outcome 'surprise' (source={SRC})"),
        ("", {"source": ""}, "unrecognized policy discovery outcome '' (source=unknown)"),
    ],
)
@pytest.mark.parametrize("knob", ["warn", "block"])
def test_trust_assertion_failures_always_block(
    outcome: str, fields: dict, reason: str, knob: str
) -> None:
    route = classify_discovery_outcome(
        _result(outcome, policy=ApmPolicy(), **fields), fetch_failure_default=knob
    )
    assert (route.kind, route.policy, route.block_reason) == ("always-block", None, reason)
    assert (route.unresolved, route.from_fetch_failure_default, route.report_miss) == (
        True,
        False,
        True,
    )


# -- outcome_cause --------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "source", "err", "cause"),
    [
        ("no_git_remote", SRC, "x", "Could not determine org from git remote"),
        ("absent", SRC, "x", f"No org policy found at {SRC}"),
        ("absent", None, None, "No org policy found at unknown"),
        ("empty", "", None, "Org policy at unknown is present but empty"),
        ("malformed", SRC, "bad yaml", "Policy fetch failed: bad yaml"),
        ("garbage_response", SRC, None, "Policy fetch failed: garbage_response"),
    ],
)
def test_outcome_cause_wording(
    outcome: str, source: str | None, err: str | None, cause: str
) -> None:
    assert outcome_cause(outcome, source, err) == cause


# -- route_discovery_outcome ------------------------------------------------------


def _logger() -> Mock:
    return Mock(spec=["policy_resolved", "policy_discovery_miss", "warning"])


@pytest.mark.parametrize("explicit", [False, True])
def test_found_announces_the_resolved_policy(explicit: bool) -> None:
    policy = ApmPolicy(enforcement="warn")
    logger = _logger()
    result = _result(
        "found",
        policy=policy,
        cached=True,
        cache_age_seconds=42,
        subgroup_scoped=True,
        chain_sources=["a", "b"],
        explicit=explicit,
    )

    assert route_discovery_outcome(result, logger=logger, fetch_failure_default="block") is policy
    logger.policy_resolved.assert_called_once_with(
        source=SRC,
        cached=True,
        enforcement="warn",
        age_seconds=42,
        subgroup_scoped=True,
        chain=["a", "b"],
        explicit=explicit,
    )
    logger.policy_discovery_miss.assert_not_called()
    logger.warning.assert_not_called()


def test_cached_stale_announces_as_cached_then_reports_the_refresh_error() -> None:
    policy = ApmPolicy(enforcement="block")
    logger = _logger()
    result = _result("cached_stale", policy=policy, error="old", fetch_error="timeout")

    assert route_discovery_outcome(result, logger=logger, fetch_failure_default="warn") is policy
    assert logger.policy_resolved.call_args.kwargs["cached"] is True
    logger.policy_discovery_miss.assert_called_once_with(
        outcome="cached_stale", source=SRC, error="timeout"
    )


@pytest.mark.parametrize(
    ("fields", "error"),
    [({"error": "e1", "fetch_error": "e2"}, "e1"), ({"fetch_error": "e2"}, "e2"), ({}, None)],
)
def test_a_warned_miss_carries_its_error_and_hint(fields: dict, error: str | None) -> None:
    logger = _logger()
    result = _result("absent", discovery_hint="hint", **fields)

    assert route_discovery_outcome(result, logger=logger, fetch_failure_default="warn") is None
    logger.policy_resolved.assert_not_called()
    logger.policy_discovery_miss.assert_called_once_with(
        outcome="absent", source=SRC, error=error, hint="hint"
    )


def test_an_always_block_outcome_reports_the_miss_then_raises() -> None:
    logger = _logger()
    with pytest.raises(PolicyViolationError) as raised:
        route_discovery_outcome(
            _result("incomplete_chain", error="gone"), logger=logger, fetch_failure_default="warn"
        )
    assert str(raised.value) == (
        f"Install blocked: org policy inheritance chain is incomplete (source={SRC}). "
        "Restore the unreachable parent policy before retrying."
    )
    assert raised.value.policy_source == SRC
    logger.policy_discovery_miss.assert_called_once_with(
        outcome="incomplete_chain", source=SRC, error="gone"
    )


def test_a_fetch_failure_block_raises_without_a_contradicting_miss_line() -> None:
    logger = _logger()
    with pytest.raises(PolicyViolationError, match=r"^Blocked: no enforceable org policy"):
        route_discovery_outcome(
            _result("absent", source=""),
            logger=logger,
            fetch_failure_default="block",
            blocked_label="Blocked",
        )
    logger.policy_discovery_miss.assert_not_called()


def test_a_block_raises_with_an_unknown_source() -> None:
    with pytest.raises(PolicyViolationError) as raised:
        route_discovery_outcome(
            _result("hash_mismatch", source=""), logger=None, fetch_failure_default="warn"
        )
    assert raised.value.policy_source == "unknown"


def test_a_dry_run_previews_the_block_instead_of_raising() -> None:
    logger = _logger()
    route = classify_discovery_outcome(_result("absent"), fetch_failure_default="block")

    policy = route_discovery_outcome(
        _result("absent", discovery_hint="h"),
        logger=logger,
        fetch_failure_default="block",
        raise_blocking_errors=False,
    )

    assert policy is None
    logger.policy_discovery_miss.assert_called_once_with(
        outcome="absent", source=SRC, error=None, hint="h"
    )
    message = logger.warning.call_args.args[0]
    assert message.startswith("Would be blocked by policy: ")
    assert (
        message.removeprefix("Would be blocked by policy: ").split(". ")[0]
        == str(route.block_reason).split(". ")[0]
    )


def test_routing_without_a_logger_still_returns_the_policy() -> None:
    policy = ApmPolicy()
    assert (
        route_discovery_outcome(
            _result("found", policy=policy), logger=None, fetch_failure_default="warn"
        )
        is policy
    )
    assert (
        route_discovery_outcome(
            _result("absent"),
            logger=None,
            fetch_failure_default="block",
            raise_blocking_errors=False,
        )
        is None
    )


# -- discovery and trust readers ----------------------------------------------------


@pytest.mark.parametrize("error", [OSError("token abc"), ValueError("x"), RuntimeError("y")])
def test_a_discovery_error_is_a_neutral_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    def broken(_root: Path) -> PolicyFetchResult:
        raise error

    monkeypatch.setattr(DISCOVERY, broken)

    result = discover_trust_policy(tmp_path)

    assert result.outcome == "cache_miss_fetch_fail"
    assert result.fetch_error == f"policy discovery failed ({type(error).__name__})"


def test_discovery_receives_the_project_root_as_a_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[object] = []
    found = _result("found", policy=ApmPolicy())
    monkeypatch.setattr(DISCOVERY, lambda root: seen.append(root) or found)

    assert discover_trust_policy(str(tmp_path)) is found
    assert seen == [tmp_path]


def _project(tmp_path: Path, knob: str | None) -> Path:
    body = "name: p\nversion: 1.0.0\n"
    if knob:
        body += f"policy:\n  fetch_failure_default: {knob}\n"
    (tmp_path / "apm.yml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(("knob", "blocks"), [("block", True), ("warn", False), (None, False)])
def test_classify_project_policy_reads_the_project_knob(
    tmp_path: Path, knob: str | None, blocks: bool
) -> None:
    route = classify_project_policy(_project(tmp_path, knob), fetch_result=_result("absent"))
    assert route.blocks is blocks


def test_classify_project_policy_discovers_when_no_result_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(DISCOVERY, lambda root: _result("hash_mismatch"))
    assert classify_project_policy(str(_project(tmp_path, None))).kind == "always-block"


def test_no_policy_skips_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(DISCOVERY, Mock(side_effect=AssertionError))
    assert resolve_trust_policy(tmp_path, no_policy=True) is None


def test_a_trust_reader_does_not_reannounce_and_reports_the_miss_after_routing(
    tmp_path: Path,
) -> None:
    logger = _logger()
    policy = ApmPolicy()
    assert (
        resolve_trust_policy(
            _project(tmp_path, None), logger=logger, fetch_result=_result("found", policy=policy)
        )
        is policy
    )
    logger.policy_resolved.assert_not_called()

    assert (
        resolve_trust_policy(
            _project(tmp_path, None),
            logger=logger,
            fetch_result=_result("absent", discovery_hint="h"),
        )
        is None
    )
    logger.policy_discovery_miss.assert_called_once_with(
        outcome="absent", source=SRC, error=None, hint="h"
    )


def test_a_trust_reader_without_announce_stays_silent(tmp_path: Path) -> None:
    logger = _logger()
    resolve_trust_policy(
        _project(tmp_path, None), logger=logger, announce=False, fetch_result=_result("malformed")
    )
    logger.policy_discovery_miss.assert_not_called()


def test_a_trust_reader_discovers_when_no_result_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = ApmPolicy()
    monkeypatch.setattr(DISCOVERY, lambda root: _result("found", policy=policy))
    assert resolve_trust_policy(_project(tmp_path, None)) is policy


def test_a_trust_reader_block_names_the_org_policy_and_reports_nothing(tmp_path: Path) -> None:
    logger = _logger()
    with pytest.raises(PolicyViolationError, match=r"^Blocked by org policy: no enforceable"):
        resolve_trust_policy(
            _project(tmp_path, "block"), logger=logger, fetch_result=_result("empty")
        )
    logger.policy_discovery_miss.assert_not_called()


def test_a_trust_reader_previews_a_block_silently_when_asked(tmp_path: Path) -> None:
    # Every dry-run trust reader passes announce=False (the command reports the miss).
    assert (
        resolve_trust_policy(
            _project(tmp_path, "block"),
            announce=False,
            raise_blocking_errors=False,
            fetch_result=_result("empty"),
        )
        is None
    )


class _PlainLogger:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose


@pytest.mark.parametrize(
    ("outcome", "fields", "verbose", "message"),
    [
        ("absent", {}, False, None),
        ("no_git_remote", {}, False, None),
        (
            "absent",
            {},
            True,
            f"Org policy not applied (outcome=absent, source={SRC})",
        ),
        (
            "malformed",
            {"error": "bad"},
            False,
            f"Org policy not applied (outcome=malformed, source={SRC}): bad",
        ),
        (
            "malformed",
            {"source": ""},
            False,
            "Org policy not applied (outcome=malformed, source=unknown)",
        ),
        (
            "cached_stale",
            {"fetch_error": "timeout"},
            False,
            "Using stale cached org policy (refresh failed: timeout); enforcement still applies.",
        ),
        (
            "cached_stale",
            {},
            False,
            "Using stale cached org policy (refresh failed); enforcement still applies.",
        ),
    ],
)
def test_a_reader_without_policy_wording_reports_the_miss_neutrally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    fields: dict,
    verbose: bool,
    message: str | None,
) -> None:
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(WARNING, lambda text, symbol=None: warnings.append((text, symbol)))
    policy = ApmPolicy() if outcome == "cached_stale" else None
    resolve_trust_policy(
        _project(tmp_path, None),
        logger=_PlainLogger(verbose),
        fetch_result=_result(outcome, policy=policy, **fields),
    )
    assert warnings == ([] if message is None else [(message, "warning")])


def test_a_neutral_reader_report_happens_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(WARNING, lambda text, symbol=None: warnings.append(text))
    reader = outcome_routing._TrustReaderLog(_PlainLogger())
    reader.policy_discovery_miss(outcome="malformed", source=SRC)
    reader.flush()
    reader.flush()
    assert warnings == [f"Org policy not applied (outcome=malformed, source={SRC})"]


# -- duck-typed results and edge text (mutation-found gaps) ---------------------


def test_a_result_without_fields_is_an_unrecognized_outcome() -> None:
    route = classify_discovery_outcome(object(), fetch_failure_default="warn")
    assert (route.outcome, route.kind, route.policy) == ("", "always-block", None)
    assert route.block_reason == "unrecognized policy discovery outcome '' (source=unknown)"


@pytest.mark.parametrize(("outcome", "kind"), [("found", "enforce"), ("disabled", "disabled")])
def test_the_route_keeps_its_outcome(outcome: str, kind: str) -> None:
    route = classify_discovery_outcome(_result(outcome), fetch_failure_default="warn")
    assert (route.outcome, route.kind) == (outcome, kind)


def test_a_partial_fetch_failure_result_still_blocks_with_its_cause() -> None:
    from types import SimpleNamespace

    route = classify_discovery_outcome(
        SimpleNamespace(outcome="malformed", source=SRC), fetch_failure_default="block"
    )
    assert route.block_reason == (
        "no enforceable org policy was resolved (outcome=malformed) and project apm.yml has "
        f"policy.fetch_failure_default=block (source={SRC}). Policy fetch failed: malformed. "
        f"{REMEDY.format(f' ({SRC})')}"
    )


def test_a_partial_ambiguous_result_names_the_remote_problem() -> None:
    from types import SimpleNamespace

    route = classify_discovery_outcome(
        SimpleNamespace(outcome="ambiguous_git_remote", source=SRC), fetch_failure_default="warn"
    )
    assert route.block_reason == "several git remotes and no 'origin'"


def test_trailing_periods_of_cause_and_hint_are_not_doubled() -> None:
    route = classify_discovery_outcome(
        _result("malformed", error="BAD BOX.", discovery_hint="Check the VPN BOX."),
        fetch_failure_default="block",
    )
    assert ". Policy fetch failed: BAD BOX. Check the VPN BOX. Run 'apm policy status'" in str(
        route.block_reason
    )


def test_a_given_fetch_result_is_classified_without_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(DISCOVERY, Mock(side_effect=AssertionError))
    route = classify_project_policy(_project(tmp_path, None), fetch_result=_result("hash_mismatch"))
    assert route.kind == "always-block"


@pytest.mark.parametrize(("outcome", "reported"), [("absent", False), ("malformed", True)])
def test_a_logger_without_verbosity_is_not_verbose(
    monkeypatch: pytest.MonkeyPatch, outcome: str, reported: bool
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(WARNING, lambda text, symbol=None: warnings.append(text))
    outcome_routing._report_miss_neutrally(object(), outcome=outcome)
    assert warnings == (
        [f"Org policy not applied (outcome={outcome}, source=unknown)"] if reported else []
    )
