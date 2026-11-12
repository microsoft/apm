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

from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.install import validation
from apm_cli.models.apm_package import DependencyReference


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

    def test_virtual_typed_throttle_allows_the_download_stage(self) -> None:
        """Virtual preflight must not turn an indeterminate throttle into False."""
        resolver = self._setup_resolver()
        dep_ref = DependencyReference(
            repo_url="octocat/hello-world",
            host="github.com",
            reference="main",
            virtual_path="instructions/example.instructions.md",
            is_virtual=True,
        )
        throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.validate_virtual_package_exists",
            side_effect=throttle,
        ):
            result = validation._validate_package_exists(
                dep_ref.to_canonical(),
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True
