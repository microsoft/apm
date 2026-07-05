"""Tests for the GitHub API rate-limit classification path in install.validation.

Bug: on a single-IP runner that concentrates many public-package installs
(e.g. the consolidated macOS release job), the accessibility probe hits
GitHub's primary (60/hr) or secondary (concurrency) rate limit and gets a
403/429. The old code treated that as "package not accessible" and aborted
the install -- a false negative, since a throttled response is no evidence
the repo is missing. After the fix a throttled probe is allowed through so
the download step becomes the source of truth, while genuine 404s and plain
permission 403s still fail closed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install import validation


class TestRateLimitHelpers:
    def test_is_rate_limit_failure_detects_marker(self):
        exc = RuntimeError("GitHub API rate limit hit for github.com (403)")
        assert validation._is_rate_limit_failure(exc) is True

    def test_is_rate_limit_failure_detects_via_cause_chain(self):
        original = RuntimeError("GitHub API rate limit hit for github.com (429)")
        wrapped = RuntimeError("authenticated retry also failed")
        wrapped.__cause__ = original
        assert validation._is_rate_limit_failure(wrapped) is True

    def test_is_rate_limit_failure_false_for_generic_errors(self):
        assert validation._is_rate_limit_failure(RuntimeError("API returned 404")) is False
        assert validation._is_rate_limit_failure(ValueError("nope")) is False

    def test_is_rate_limit_failure_bounded_chain_walk(self):
        exc = RuntimeError("oops")
        exc.__cause__ = exc
        assert validation._is_rate_limit_failure(exc) is False

    def test_raise_if_rate_limited_primary_exhaustion(self):
        resp = MagicMock(status_code=403, headers={"X-RateLimit-Remaining": "0"})
        with pytest.raises(RuntimeError, match=r"GitHub API rate limit"):
            validation._raise_if_rate_limited(resp, "github.com")

    def test_raise_if_rate_limited_secondary_retry_after(self):
        resp = MagicMock(status_code=403, headers={"Retry-After": "60"})
        with pytest.raises(RuntimeError, match=r"GitHub API rate limit"):
            validation._raise_if_rate_limited(resp, "github.com")

    def test_raise_if_rate_limited_http_429(self):
        resp = MagicMock(status_code=429, headers={})
        with pytest.raises(RuntimeError, match=r"GitHub API rate limit"):
            validation._raise_if_rate_limited(resp, "github.com")

    def test_raise_if_rate_limited_ignores_plain_403(self):
        # SSO / permission 403 carries no rate-limit signal -> no raise.
        resp = MagicMock(status_code=403, headers={"X-RateLimit-Remaining": "4900"})
        validation._raise_if_rate_limited(resp, "github.com")

    def test_raise_if_rate_limited_ignores_404(self):
        resp = MagicMock(status_code=404, headers={"Retry-After": "60"})
        validation._raise_if_rate_limited(resp, "github.com")


class TestValidateRateLimitClassification:
    """End-to-end: a throttled probe returns True (allow-through)."""

    def _setup_resolver(self, token=None):
        resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        host_info.kind = "github"
        host_info.has_public_repos = True
        resolver.classify_host.return_value = host_info
        ctx = MagicMock(source="env", token_type="pat", token=token)
        resolver.resolve.return_value = ctx
        resolver.resolve_for_dep.return_value = ctx
        return resolver

    def test_throttled_probe_allows_through(self):
        """Both anon and token attempts hit the limit -> proceed to download."""
        resolver = self._setup_resolver(token="ghp_fake")

        def _throttled_fallback(host, op, **kwargs):
            try:
                return op(None, {})
            except Exception:
                # Authenticated retry also throttled; exception propagates.
                return op("ghp_fake", {})

        resolver.try_with_fallback.side_effect = _throttled_fallback

        logger = MagicMock()
        rate_limited = MagicMock(
            ok=False,
            status_code=403,
            reason="Forbidden",
            headers={"X-RateLimit-Remaining": "0"},
        )

        with patch("apm_cli.install.validation.requests.get", return_value=rate_limited):
            result = validation._validate_package_exists(
                "octocat/hello-world",
                verbose=False,
                auth_resolver=resolver,
                logger=logger,
            )

        assert result is True

    def test_plain_403_still_returns_false(self):
        """Permission 403 (no rate-limit signal) must still fail closed."""
        resolver = self._setup_resolver()
        resolver.try_with_fallback.side_effect = lambda host, op, **kw: op(None, {})

        forbidden = MagicMock(
            ok=False,
            status_code=403,
            reason="Forbidden",
            headers={"X-RateLimit-Remaining": "4900"},
        )

        with patch("apm_cli.install.validation.requests.get", return_value=forbidden):
            result = validation._validate_package_exists(
                "octocat/private-repo", verbose=False, auth_resolver=resolver
            )

        assert result is False
