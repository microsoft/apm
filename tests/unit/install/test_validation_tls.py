"""Tests for the TLS-failure classification path in install.validation.

Bug: behind a TLS-intercepting corporate proxy, the validator (which used
to use stdlib urllib) ignored REQUESTS_CA_BUNDLE and surfaced a misleading
"package not accessible" error.  After the fix, validation goes through
``requests`` and an ``SSLError`` is logged with a CA-trust hint in the
verbose stream so users debugging the failure see the right cause.
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
    """End-to-end: SSLError from requests.get -> False return + verbose hint."""

    def _setup_resolver(self):
        """Build an AuthResolver-like mock that exercises the unauth path only."""
        resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        host_info.kind = "github"
        resolver.classify_host.return_value = host_info
        ctx = MagicMock(source="env", token_type="pat", token=None)
        resolver.resolve.return_value = ctx
        resolver.resolve_for_dep.return_value = ctx

        # try_with_fallback should call the operation once with token=None and
        # let the SSLError propagate so the outer except can classify it.
        def _fake_fallback(host, op, **kwargs):
            return op(None, {})

        resolver.try_with_fallback.side_effect = _fake_fallback
        return resolver

    def _capture_verbose(self):
        """Build a logger mock whose verbose_detail captures all messages."""
        captured: list[str] = []
        logger = MagicMock()
        logger.verbose = True
        logger.verbose_detail.side_effect = lambda msg: captured.append(msg)
        return logger, captured

    def test_ssl_error_returns_false_and_logs_ca_hint_to_verbose(self):
        resolver = self._setup_resolver()
        logger, captured = self._capture_verbose()

        with patch(
            "requests.get",
            side_effect=requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
        ):
            result = validation._validate_package_exists(
                "octocat/hello-world",
                verbose=True,
                auth_resolver=resolver,
                logger=logger,
            )

        assert result is False
        joined = "\n".join(captured)
        assert "TLS verification failed" in joined
        assert "REQUESTS_CA_BUNDLE" in joined

    def test_ssl_error_emits_nothing_when_not_verbose(self):
        """Default (non-verbose) output must stay quiet -- the orchestrator
        renders the user-facing 'not accessible' line; the TLS detail lives
        behind --verbose."""
        resolver = self._setup_resolver()

        with patch(
            "requests.get",
            side_effect=requests.exceptions.SSLError("bad cert"),
        ):
            result = validation._validate_package_exists(
                "octocat/hello-world", verbose=False, auth_resolver=resolver
            )

        assert result is False

    def test_ssl_error_skips_auth_error_context(self):
        """TLS failures must not render the PAT/auth troubleshooting wall."""
        resolver = self._setup_resolver()
        logger, _captured = self._capture_verbose()

        with patch(
            "requests.get",
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

        with patch("requests.get", return_value=fake_resp):
            result = validation._validate_package_exists(
                "octocat/missing", verbose=False, auth_resolver=resolver
            )

        assert result is False

    def test_http_200_returns_true(self):
        resolver = self._setup_resolver()
        fake_resp = MagicMock(ok=True, status_code=200, reason="OK")

        with patch("requests.get", return_value=fake_resp):
            result = validation._validate_package_exists(
                "octocat/hello-world", verbose=False, auth_resolver=resolver
            )

        assert result is True


class TestNoUrllibUrlopenInValidation:
    """Regression guard: keep the validator on requests, not urllib."""

    def test_validation_module_does_not_import_urllib_request_urlopen(self):
        from pathlib import Path

        src = Path(validation.__file__).read_text(encoding="utf-8")
        # Forbid the specific call form; importing urllib for other reasons
        # remains acceptable.
        assert "urllib.request.urlopen" not in src, (
            "install/validation.py must use 'requests' for HTTP probes so it "
            "honours REQUESTS_CA_BUNDLE the same way the rest of the codebase "
            "does. Replace urllib.request.urlopen with requests.get."
        )
