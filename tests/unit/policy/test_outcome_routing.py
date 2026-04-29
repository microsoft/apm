"""Unit tests for ``apm_cli.policy.outcome_routing.route_discovery_outcome``.

Covers the full 9-outcome routing table, fail-open vs fail-closed rules,
logger call contracts, and the raise_blocking_errors escape hatch.

Security properties verified:
- hash_mismatch is ALWAYS fail-closed regardless of fetch_failure_default.
- absent / no_git_remote / empty are ALWAYS fail-open (not in _FETCH_FAILURE_OUTCOMES).
- malformed / cache_miss_fetch_fail / garbage_response respect fetch_failure_default.
- cached_stale respects the policy's own fetch_failure knob.
- disabled short-circuits without any logging.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.outcome_routing import (
    _FETCH_FAILURE_OUTCOMES,
    _NON_FOUND_LOGGED_OUTCOMES,
    route_discovery_outcome,
)
from apm_cli.policy.schema import ApmPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    """Return a mock logger with policy-discovery method stubs."""
    logger = MagicMock()
    logger.policy_discovery_miss = MagicMock()
    logger.policy_resolved = MagicMock()
    return logger


def _make_policy(enforcement: str = "warn", fetch_failure: str = "warn") -> ApmPolicy:
    return ApmPolicy(enforcement=enforcement, fetch_failure=fetch_failure)


def _make_result(outcome: str, **kwargs) -> PolicyFetchResult:
    return PolicyFetchResult(outcome=outcome, source="org:acme/.github", **kwargs)


# ===========================================================================
# Module-level constants
# ===========================================================================


class TestModuleConstants:
    """Verify the published constant sets used by callers."""

    def test_fetch_failure_outcomes_are_subset_of_non_found(self):
        for o in _FETCH_FAILURE_OUTCOMES:
            assert o in _NON_FOUND_LOGGED_OUTCOMES

    def test_always_fail_open_outcomes_not_in_fetch_failure(self):
        """absent / no_git_remote / empty must NEVER be in the fail-closed set."""
        for o in ("absent", "no_git_remote", "empty"):
            assert o not in _FETCH_FAILURE_OUTCOMES


# ===========================================================================
# disabled outcome
# ===========================================================================


class TestDisabledOutcome:
    """'disabled' is a no-op: return None, no logging."""

    def test_returns_none(self):
        fetch = _make_result("disabled")
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is None

    def test_no_logger_calls(self):
        logger = _make_logger()
        fetch = _make_result("disabled")
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_discovery_miss.assert_not_called()
        logger.policy_resolved.assert_not_called()

    def test_none_logger_accepted(self):
        fetch = _make_result("disabled")
        result = route_discovery_outcome(
            fetch, logger=None, fetch_failure_default="warn"
        )
        assert result is None


# ===========================================================================
# hash_mismatch outcome
# ===========================================================================


class TestHashMismatchOutcome:
    """hash_mismatch is always fail-closed regardless of config."""

    def test_raises_policy_violation_error(self):
        fetch = _make_result("hash_mismatch")
        with pytest.raises(PolicyViolationError, match="hash mismatch"):
            route_discovery_outcome(
                fetch, logger=_make_logger(), fetch_failure_default="warn"
            )

    def test_raises_even_with_fetch_failure_default_warn(self):
        fetch = _make_result("hash_mismatch")
        with pytest.raises(PolicyViolationError):
            route_discovery_outcome(
                fetch, logger=None, fetch_failure_default="warn"
            )

    def test_raise_blocking_errors_false_returns_none(self):
        """Dry-run callers skip raising and get None back."""
        fetch = _make_result("hash_mismatch")
        result = route_discovery_outcome(
            fetch,
            logger=_make_logger(),
            fetch_failure_default="warn",
            raise_blocking_errors=False,
        )
        assert result is None

    def test_logs_policy_discovery_miss(self):
        logger = _make_logger()
        fetch = _make_result("hash_mismatch", error="sha256:abc != sha256:def")
        with pytest.raises(PolicyViolationError):
            route_discovery_outcome(
                fetch, logger=logger, fetch_failure_default="warn"
            )
        logger.policy_discovery_miss.assert_called_once()
        kwargs = logger.policy_discovery_miss.call_args[1]
        assert kwargs["outcome"] == "hash_mismatch"

    def test_none_logger_no_crash(self):
        fetch = _make_result("hash_mismatch")
        with pytest.raises(PolicyViolationError):
            route_discovery_outcome(fetch, logger=None, fetch_failure_default="warn")

    def test_error_message_contains_source(self):
        fetch = PolicyFetchResult(
            outcome="hash_mismatch", source="org:contoso/.github"
        )
        with pytest.raises(PolicyViolationError, match="contoso"):
            route_discovery_outcome(
                fetch, logger=None, fetch_failure_default="warn"
            )


# ===========================================================================
# Always-fail-open outcomes: absent, no_git_remote, empty
# ===========================================================================


class TestAlwaysFailOpenOutcomes:
    """absent / no_git_remote / empty are always fail-open.

    Even when fetch_failure_default='block', these outcomes return None
    without raising because they are not network-fetch failures.
    """

    @pytest.mark.parametrize("outcome", ["absent", "no_git_remote", "empty"])
    def test_returns_none_with_warn(self, outcome):
        fetch = _make_result(outcome)
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is None

    @pytest.mark.parametrize("outcome", ["absent", "no_git_remote", "empty"])
    def test_returns_none_with_block(self, outcome):
        """Must NOT raise even when project sets fetch_failure_default=block."""
        fetch = _make_result(outcome)
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="block"
        )
        assert result is None

    @pytest.mark.parametrize("outcome", ["absent", "no_git_remote", "empty"])
    def test_logs_policy_discovery_miss(self, outcome):
        logger = _make_logger()
        fetch = _make_result(outcome)
        route_discovery_outcome(
            fetch, logger=logger, fetch_failure_default="warn"
        )
        logger.policy_discovery_miss.assert_called_once()
        kwargs = logger.policy_discovery_miss.call_args[1]
        assert kwargs["outcome"] == outcome

    @pytest.mark.parametrize("outcome", ["absent", "no_git_remote", "empty"])
    def test_no_policy_resolved_logged(self, outcome):
        logger = _make_logger()
        fetch = _make_result(outcome)
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_resolved.assert_not_called()


# ===========================================================================
# Fetch-failure outcomes: malformed, cache_miss_fetch_fail, garbage_response
# ===========================================================================


class TestFetchFailureOutcomes:
    """malformed / cache_miss_fetch_fail / garbage_response respect fetch_failure_default."""

    @pytest.mark.parametrize(
        "outcome",
        ["malformed", "cache_miss_fetch_fail", "garbage_response"],
    )
    def test_warn_mode_returns_none(self, outcome):
        fetch = _make_result(outcome, error="oops")
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is None

    @pytest.mark.parametrize(
        "outcome",
        ["malformed", "cache_miss_fetch_fail", "garbage_response"],
    )
    def test_block_mode_raises(self, outcome):
        fetch = _make_result(outcome, error="network error")
        with pytest.raises(PolicyViolationError):
            route_discovery_outcome(
                fetch, logger=_make_logger(), fetch_failure_default="block"
            )

    @pytest.mark.parametrize(
        "outcome",
        ["malformed", "cache_miss_fetch_fail", "garbage_response"],
    )
    def test_block_mode_raise_blocking_false_returns_none(self, outcome):
        """Dry-run path: block config + raise_blocking_errors=False -> no raise."""
        fetch = _make_result(outcome)
        result = route_discovery_outcome(
            fetch,
            logger=_make_logger(),
            fetch_failure_default="block",
            raise_blocking_errors=False,
        )
        assert result is None

    @pytest.mark.parametrize(
        "outcome",
        ["malformed", "cache_miss_fetch_fail", "garbage_response"],
    )
    def test_logs_discovery_miss_in_warn(self, outcome):
        logger = _make_logger()
        fetch = _make_result(outcome)
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_discovery_miss.assert_called_once()
        kwargs = logger.policy_discovery_miss.call_args[1]
        assert kwargs["outcome"] == outcome

    @pytest.mark.parametrize(
        "outcome",
        ["malformed", "cache_miss_fetch_fail", "garbage_response"],
    )
    def test_none_logger_warn_no_crash(self, outcome):
        fetch = _make_result(outcome)
        result = route_discovery_outcome(
            fetch, logger=None, fetch_failure_default="warn"
        )
        assert result is None


# ===========================================================================
# cached_stale outcome
# ===========================================================================


class TestCachedStaleOutcome:
    """cached_stale: enforce using cached policy, but log both resolved + miss."""

    def _make_stale(self, fetch_failure: str = "warn") -> PolicyFetchResult:
        return PolicyFetchResult(
            outcome="cached_stale",
            source="org:acme/.github",
            policy=_make_policy(fetch_failure=fetch_failure),
            fetch_error="timeout",
            cache_age_seconds=7200,
        )

    def test_returns_policy(self):
        fetch = self._make_stale()
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is fetch.policy

    def test_logs_policy_resolved(self):
        logger = _make_logger()
        fetch = self._make_stale()
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_resolved.assert_called_once()

    def test_logs_discovery_miss(self):
        logger = _make_logger()
        fetch = self._make_stale()
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_discovery_miss.assert_called_once()
        kwargs = logger.policy_discovery_miss.call_args[1]
        assert kwargs["outcome"] == "cached_stale"

    def test_policy_fetch_failure_block_raises(self):
        """cached_stale with policy.fetch_failure=block is fail-closed."""
        fetch = self._make_stale(fetch_failure="block")
        with pytest.raises(PolicyViolationError, match="refresh failed"):
            route_discovery_outcome(
                fetch, logger=_make_logger(), fetch_failure_default="warn"
            )

    def test_policy_fetch_failure_warn_no_raise(self):
        """cached_stale with policy.fetch_failure=warn is fail-open."""
        fetch = self._make_stale(fetch_failure="warn")
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is not None

    def test_raise_blocking_errors_false_skips_raise(self):
        """Dry-run: fetch_failure=block but raise_blocking_errors=False -> return policy."""
        fetch = self._make_stale(fetch_failure="block")
        result = route_discovery_outcome(
            fetch,
            logger=_make_logger(),
            fetch_failure_default="warn",
            raise_blocking_errors=False,
        )
        # Should not raise; returns the stale policy
        assert result is fetch.policy

    def test_none_policy_no_crash_on_resolved_log(self):
        """If the stale result has no policy object, resolved log is skipped."""
        fetch = PolicyFetchResult(
            outcome="cached_stale",
            source="org:acme/.github",
            policy=None,
            fetch_error="timeout",
        )
        logger = _make_logger()
        result = route_discovery_outcome(
            fetch, logger=logger, fetch_failure_default="warn"
        )
        assert result is None
        logger.policy_resolved.assert_not_called()

    def test_none_logger_no_crash(self):
        fetch = self._make_stale()
        result = route_discovery_outcome(
            fetch, logger=None, fetch_failure_default="warn"
        )
        assert result is fetch.policy


# ===========================================================================
# found outcome
# ===========================================================================


class TestFoundOutcome:
    """'found' is the happy path: return policy, log policy_resolved."""

    def test_returns_policy(self):
        policy = _make_policy()
        fetch = _make_result("found", policy=policy, cached=False, cache_age_seconds=0)
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is policy

    def test_logs_policy_resolved(self):
        logger = _make_logger()
        policy = _make_policy()
        fetch = _make_result("found", policy=policy)
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_resolved.assert_called_once()

    def test_no_discovery_miss_logged(self):
        logger = _make_logger()
        policy = _make_policy()
        fetch = _make_result("found", policy=policy)
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_discovery_miss.assert_not_called()

    def test_none_policy_returns_none(self):
        """found with policy=None is unusual but handled defensively."""
        fetch = _make_result("found", policy=None)
        logger = _make_logger()
        result = route_discovery_outcome(
            fetch, logger=logger, fetch_failure_default="warn"
        )
        assert result is None
        logger.policy_resolved.assert_not_called()

    def test_none_logger_no_crash(self):
        policy = _make_policy()
        fetch = _make_result("found", policy=policy)
        result = route_discovery_outcome(
            fetch, logger=None, fetch_failure_default="warn"
        )
        assert result is policy

    def test_cached_flag_passed_to_logger(self):
        logger = _make_logger()
        policy = _make_policy()
        fetch = _make_result("found", policy=policy, cached=True, cache_age_seconds=120)
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        kwargs = logger.policy_resolved.call_args[1]
        assert kwargs["cached"] is True
        assert kwargs["age_seconds"] == 120


# ===========================================================================
# Unknown / unrecognised outcome
# ===========================================================================


class TestUnknownOutcome:
    """Defensive path: unknown outcomes return None without crashing."""

    def test_returns_none_for_unknown_outcome(self):
        fetch = _make_result("some_future_outcome")
        result = route_discovery_outcome(
            fetch, logger=_make_logger(), fetch_failure_default="warn"
        )
        assert result is None

    def test_no_logging_for_unknown_outcome(self):
        logger = _make_logger()
        fetch = _make_result("some_future_outcome")
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        logger.policy_discovery_miss.assert_not_called()
        logger.policy_resolved.assert_not_called()


# ===========================================================================
# Cross-cutting: fetch_error vs error field priority
# ===========================================================================


class TestErrorFieldRouting:
    """Verify that both error and fetch_error fields reach the logger."""

    def test_error_field_passed_for_hash_mismatch(self):
        logger = _make_logger()
        fetch = _make_result("hash_mismatch", error="sha mismatch detail")
        with pytest.raises(PolicyViolationError):
            route_discovery_outcome(
                fetch, logger=logger, fetch_failure_default="warn"
            )
        kwargs = logger.policy_discovery_miss.call_args[1]
        assert kwargs.get("error") == "sha mismatch detail"

    def test_fetch_error_field_passed_for_malformed(self):
        logger = _make_logger()
        fetch = _make_result("malformed", fetch_error="YAML parse error")
        route_discovery_outcome(fetch, logger=logger, fetch_failure_default="warn")
        kwargs = logger.policy_discovery_miss.call_args[1]
        # error kwarg = fetch_result.error or fetch_result.fetch_error
        assert kwargs.get("error") == "YAML parse error"
