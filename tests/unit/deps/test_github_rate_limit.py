"""Mutation-resistant tests for GitHub's typed throttle classifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.deps.github_rate_limit import (
    GitHubThrottleError,
    classify_github_throttle,
    github_throttle_error,
)


def _response(status_code: int, headers: dict[str, str] | None = None) -> SimpleNamespace:
    """Build the smallest response shape consumed by the classifier."""
    return SimpleNamespace(status_code=status_code, headers=headers or {})


@pytest.mark.parametrize(
    ("status_code", "headers", "signal"),
    (
        (429, {}, "http-429"),
        (403, {"X-RateLimit-Remaining": "0"}, "remaining-zero"),
        (403, {"Retry-After": "60"}, "retry-after"),
    ),
)
def test_classifier_accepts_only_each_confirmed_throttle_signal(
    status_code: int,
    headers: dict[str, str],
    signal: str,
) -> None:
    """Each positive branch is independently load-bearing."""
    throttle = classify_github_throttle(status_code, headers)

    assert throttle is not None
    assert throttle.signal == signal


@pytest.mark.parametrize(
    ("status_code", "headers"),
    (
        (401, {"X-RateLimit-Remaining": "0"}),
        (403, {}),
        (403, {"X-RateLimit-Remaining": "1"}),
        (403, {"X-RateLimit-Remaining": "invalid"}),
        (403, {"Retry-After": "0"}),
        (403, {"Retry-After": "invalid"}),
        (403, {"Retry-After": "inf"}),
        (403, {"Retry-After": "1e309"}),
        (404, {"X-RateLimit-Remaining": "0"}),
    ),
)
def test_classifier_rejects_auth_missing_and_ambiguous_responses(
    status_code: int,
    headers: dict[str, str],
) -> None:
    """No non-confirmed response can unlock the sparse-Git fallback."""
    assert classify_github_throttle(status_code, headers) is None


def test_typed_error_contains_no_response_header_or_credential_value() -> None:
    """The public error contains only the safe host and status."""
    response = _response(403, {"Retry-After": "60", "Authorization": "secret-value"})

    error = github_throttle_error(response, "github.com")

    assert isinstance(error, GitHubThrottleError)
    assert str(error) == "GitHub API throttle for github.com (HTTP 403)"
