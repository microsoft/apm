"""Typed, token-free classification of GitHub API throttles."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class GitHubThrottle:
    """A confirmed GitHub API throttle, without credentials or response body."""

    status_code: int
    signal: str
    retry_after_seconds: float | None = None


class GitHubThrottleError(RuntimeError):
    """Raised when a GitHub API response conclusively signals throttling."""

    def __init__(self, throttle: GitHubThrottle, host: str) -> None:
        self.throttle = throttle
        self.host = host
        super().__init__(f"GitHub API throttle for {host} (HTTP {throttle.status_code})")


def classify_github_throttle(
    status_code: int,
    headers: Mapping[str, str] | None,
) -> GitHubThrottle | None:
    """Return a throttle only for GitHub's unambiguous exhaustion signals."""
    values = headers or {}
    retry_after_seconds = _positive_retry_after_seconds(values)
    if status_code == 429:
        return GitHubThrottle(status_code, "http-429", retry_after_seconds)
    if status_code != 403:
        return None

    remaining = values.get("X-RateLimit-Remaining")
    if isinstance(remaining, str) and remaining.strip() == "0":
        return GitHubThrottle(status_code, "remaining-zero")

    if retry_after_seconds is not None:
        return GitHubThrottle(status_code, "retry-after", retry_after_seconds)
    return None


def _positive_retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Return a finite positive Retry-After value, or ``None`` when absent/invalid."""
    retry_after = headers.get("Retry-After")
    if not isinstance(retry_after, str):
        return None
    try:
        delay = float(retry_after)
    except ValueError:
        return None
    return delay if math.isfinite(delay) and delay > 0 else None


def github_throttle_error(response: object, host: str) -> GitHubThrottleError | None:
    """Create the typed throttle error for a response, or return ``None``."""
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        return None
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        headers = None
    throttle = classify_github_throttle(status_code, headers)
    return GitHubThrottleError(throttle, host) if throttle is not None else None


def raise_for_github_throttle(response: object, host: str) -> None:
    """Raise ``GitHubThrottleError`` when *response* is conclusively throttled."""
    error = github_throttle_error(response, host)
    if error is not None:
        raise error
