"""Tests for the TLS-failure classification path in install.validation.

Bug: behind a TLS-intercepting corporate proxy, the validator (which used
to use stdlib urllib) ignored REQUESTS_CA_BUNDLE and surfaced a misleading
"package not accessible" error.  After the fix, validation goes through
``requests``, an ``SSLError`` raises a wrapped ``RuntimeError``, and the
outer handler logs a single CA-trust hint -- visible at default verbosity
so users behind a corporate proxy don't have to re-run with --verbose.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.install import validation


class TestTlsHelpers:
    def test_is_tls_failure_detects_runtime_error_marker(self):
        exc = RuntimeError("TLS verification failed for github.com")
        assert validation._is_tls_failure(exc) is True

    def test_is_tls_failure_detects_certificate_verify_failed(self):
        exc = RuntimeError("ssl error: CERTIFICATE_VERIFY_FAILED")
        assert validation._is_tls_failure(exc) is True

    def test_is_tls_failure_detects_ssl_error_via_cause_chain(self):
        original = requests.exceptions.SSLError("bad cert")
        wrapped = RuntimeError("API request failed")
        wrapped.__cause__ = original
        assert validation._is_tls_failure(wrapped) is True

    def test_is_tls_failure_returns_false_for_generic_errors(self):
        assert validation._is_tls_failure(RuntimeError("API returned 404")) is False
        assert validation._is_tls_failure(ValueError("nope")) is False

    def test_is_tls_failure_bounded_chain_walk(self):
        # Self-referential chain must not loop forever.
        exc = RuntimeError("oops")
        exc.__cause__ = exc
        assert validation._is_tls_failure(exc) is False


class TestValidateTlsClassification:
    """End-to-end: SSLError from requests.get -> False return + single CA hint."""

    def _setup_resolver(self, token=None):
        """Build an AuthResolver-like mock that exercises the unauth path."""
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

        # Single-call shim: invoke the operation once unauth and let the
        # SSLError propagate so the outer except can classify it.
        def _fake_fallback(host, op, **kwargs):
            return op(None, {})

        resolver.try_with_fallback.side_effect = _fake_fallback
        return resolver

    def _capture_logger(self):
        """Build a logger mock capturing both verbose_detail and warning."""
        verbose_msgs: list[str] = []
        warning_msgs: list[str] = []
        logger = MagicMock()
        logger.verbose = True
        logger.verbose_detail.side_effect = lambda msg: verbose_msgs.append(msg)
        logger.warning.side_effect = lambda msg: warning_msgs.append(msg)
        return logger, verbose_msgs, warning_msgs

    def test_ssl_error_returns_false_and_logs_ca_hint_to_verbose(self):
        resolver = self._setup_resolver()
        logger, verbose_msgs, warning_msgs = self._capture_logger()

        with patch(
            "apm_cli.install.validation.requests.get",
            side_effect=requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
        ):
            result = validation._validate_package_exists(
                "octocat/hello-world",
                verbose=True,
                auth_resolver=resolver,
                logger=logger,
            )

        assert result is False
        joined_verbose = "\n".join(verbose_msgs)
        joined_warning = "\n".join(warning_msgs)
        # Verbose adds the host + underlying exception only; the actionable
        # REQUESTS_CA_BUNDLE hint lives on the always-on warning channel so
        # it isn't restated in the verbose detail.
        assert "underlying error from github.com" in joined_verbose
        assert "REQUESTS_CA_BUNDLE" not in joined_verbose
        assert "REQUESTS_CA_BUNDLE" in joined_warning

    def test_ssl_error_emits_actionable_hint_at_default_verbosity(self):
        """Default verbosity must surface a single one-liner so users behind
        a corporate TLS-intercepting proxy don't need to re-run with --verbose
        to see what to fix."""
        resolver = self._setup_resolver()
        logger, verbose_msgs, warning_msgs = self._capture_logger()
        logger.verbose = False

        with patch(
            "apm_cli.install.validation.requests.get",
            side_effect=requests.exceptions.SSLError("bad cert"),
        ):
            result = validation._validate_package_exists(
                "octocat/hello-world",
                verbose=False,
                auth_resolver=resolver,
                logger=logger,
            )

        assert result is False
        # One-liner via logger.warning at default verbosity, not buried in
        # verbose detail.
        assert len(warning_msgs) == 1
        assert "REQUESTS_CA_BUNDLE" in warning_msgs[0]
        # Verbose-only details should not fire at default verbosity.
        assert verbose_msgs == []

    def test_ssl_error_logs_hint_exactly_once_when_token_present(self):
        """Regression guard: try_with_fallback may retry (unauth -> token ->
        credential-fill).  TLS doesn't care about auth, so the inner SSLError
        fires on every attempt -- but the user must see the CA-trust hint
        only once, not 2-3 times."""
        # Token present means the unauth -> token retry path is exercised.
        resolver = self._setup_resolver(token="ghp_fake")

        def _retrying_fallback(host, op, **kwargs):
            try:
                return op(None, {})
            except Exception:
                try:
                    return op("ghp_fake", {})
                except Exception:
                    return op("ghp_credfill", {})

        resolver.try_with_fallback.side_effect = _retrying_fallback

        logger, _verbose_msgs, warning_msgs = self._capture_logger()

        with patch(
            "apm_cli.install.validation.requests.get",
            side_effect=requests.exceptions.SSLError("bad cert"),
        ):
            result = validation._validate_package_exists(
                "octocat/hello-world",
                verbose=False,
                auth_resolver=resolver,
                logger=logger,
            )

        assert result is False
        # Exactly one warning, regardless of how many internal retries fired.
        assert len(warning_msgs) == 1, (
            f"expected single CA hint, got {len(warning_msgs)}: {warning_msgs}"
        )

    def test_ssl_error_skips_auth_error_context(self):
        """TLS failures must not render the PAT/auth troubleshooting wall."""
        resolver = self._setup_resolver()
        logger, _verbose_msgs, _warning_msgs = self._capture_logger()

        with patch(
            "apm_cli.install.validation.requests.get",
            side_effect=requests.exceptions.SSLError("bad cert"),
        ):
            validation._validate_package_exists(
                "octocat/hello-world",
                verbose=True,
                auth_resolver=resolver,
                logger=logger,
            )

        # build_error_context emits PAT/SSO advice; on TLS failures we skip it.
        resolver.build_error_context.assert_not_called()

    def test_http_404_still_returns_false(self):
        """Regression guard: non-TLS failures keep the old behaviour."""
        resolver = self._setup_resolver()
        fake_resp = MagicMock(ok=False, status_code=404, reason="Not Found")

        with patch("apm_cli.install.validation.requests.get", return_value=fake_resp):
            result = validation._validate_package_exists(
                "octocat/missing", verbose=False, auth_resolver=resolver
            )

        assert result is False

    def test_http_200_returns_true(self):
        resolver = self._setup_resolver()
        fake_resp = MagicMock(ok=True, status_code=200, reason="OK")

        with patch("apm_cli.install.validation.requests.get", return_value=fake_resp):
            result = validation._validate_package_exists(
                "octocat/hello-world", verbose=False, auth_resolver=resolver
            )

        assert result is True


class TestNoUrllibUrlopenInValidation:
    """Regression guard: keep the validator on requests, not urllib."""

    def test_validation_does_not_call_urllib_request_urlopen(self):
        """Runtime check: if validation.py ever reaches for
        urllib.request.urlopen for HTTP probes, this assertion trips.
        More robust than substring-matching the source."""
        resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        host_info.kind = "github"
        host_info.has_public_repos = True
        resolver.classify_host.return_value = host_info
        ctx = MagicMock(source="env", token_type="pat", token=None)
        resolver.resolve.return_value = ctx
        resolver.resolve_for_dep.return_value = ctx
        resolver.try_with_fallback.side_effect = lambda host, op, **kw: op(None, {})

        fake_resp = MagicMock(ok=True, status_code=200, reason="OK")
        forbidden = AssertionError(
            "install/validation.py must use 'requests' for HTTP probes so it "
            "honours REQUESTS_CA_BUNDLE the same way the rest of the codebase "
            "does. Replace urllib.request.urlopen with requests.get."
        )

        with patch("urllib.request.urlopen", side_effect=forbidden) as urlopen_mock, \
             patch("apm_cli.install.validation.requests.get", return_value=fake_resp):
            result = validation._validate_package_exists(
                "octocat/hello-world", verbose=False, auth_resolver=resolver
            )

        assert result is True
        urlopen_mock.assert_not_called()
