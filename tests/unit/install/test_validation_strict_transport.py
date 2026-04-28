"""Tests for strict-by-default transport selection in install.validation.

Regression: ``apm install https://corp-bitbucket.example/...`` used to fall
back to SSH on port 22 when the HTTPS probe failed, masking the real HTTPS
failure (auth/redirect) behind a 30s SSH timeout (issue microsoft/apm#992).
After the fix, an explicit ``http://`` / ``https://`` / ``ssh://`` URL on a
generic host probes ONLY that transport unless ``APM_ALLOW_PROTOCOL_FALLBACK=1``
re-enables the legacy permissive chain (mirroring ``_clone_with_fallback``).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install import validation


def _make_resolver():
    """Resolver mock sufficient for the generic-host validation branch."""
    resolver = MagicMock()
    host_info = MagicMock()
    host_info.api_base = "https://bitbucket.example.internal"
    host_info.display_name = "bitbucket.example.internal"
    host_info.kind = "generic"
    host_info.has_public_repos = False
    resolver.classify_host.return_value = host_info
    ctx = MagicMock(source="env", token_type="pat", token=None)
    resolver.resolve.return_value = ctx
    resolver.resolve_for_dep.return_value = ctx
    return resolver


def _failed_run(stderr: str = "ssh: connect to host port 22: Connection timed out"):
    return subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr=stderr,
    )


def _scheme_of(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else "ssh"


class TestStrictTransportValidation:
    """Generic-host validation must honor explicit URL schemes strictly."""

    def _probe_urls(self, mock_run) -> list:
        return [call.args[0][-1] for call in mock_run.call_args_list]

    def test_explicit_https_url_does_not_fall_back_to_ssh(self, monkeypatch):
        """https:// generic dep probes ONLY HTTPS (issue #992 regression)."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch(
            "subprocess.run",
            return_value=_failed_run("fatal: Authentication failed"),
        ) as mock_run:
            result = validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/example-repo.git",
                verbose=False,
                auth_resolver=resolver,
            )
        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 1, f"explicit https:// must be strict, got {urls!r}"
        assert _scheme_of(urls[0]) == "https"

    def test_explicit_http_url_does_not_fall_back_to_ssh(self, monkeypatch):
        """Insecure http:// stays on HTTP only when allow-insecure was used."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch(
            "subprocess.run",
            return_value=_failed_run("fatal: server hung up"),
        ) as mock_run:
            result = validation._validate_package_exists(
                "http://bitbucket.example.internal/scm/team/example-repo.git",
                verbose=False,
                auth_resolver=resolver,
            )
        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 1, f"explicit http:// must be strict, got {urls!r}"
        assert _scheme_of(urls[0]) == "http"

    def test_explicit_ssh_url_does_not_fall_back_to_https(self, monkeypatch):
        """ssh:// generic dep probes ONLY SSH."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch(
            "subprocess.run",
            return_value=_failed_run("ssh: permission denied (publickey)"),
        ) as mock_run:
            result = validation._validate_package_exists(
                "ssh://git@bitbucket.example.internal/scm/team/example-repo.git",
                verbose=False,
                auth_resolver=resolver,
            )
        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 1, f"explicit ssh:// must be strict, got {urls!r}"
        assert _scheme_of(urls[0]) == "ssh"

    def test_shorthand_keeps_legacy_ssh_then_https_chain(self, monkeypatch):
        """No explicit scheme = no user preference; keep SSH-first chain."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch(
            "subprocess.run",
            return_value=_failed_run("could not read from remote"),
        ) as mock_run:
            result = validation._validate_package_exists(
                "bitbucket.example.internal/scm/team/example-repo",
                verbose=False,
                auth_resolver=resolver,
            )
        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 2, f"shorthand should chain both transports, got {urls!r}"
        assert _scheme_of(urls[0]) == "ssh"
        assert _scheme_of(urls[1]) == "https"

    def test_allow_protocol_fallback_env_restores_legacy_chain(self, monkeypatch):
        """APM_ALLOW_PROTOCOL_FALLBACK=1 re-appends the opposite scheme so the
        validation pre-check matches the (also-permissive) clone path."""
        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "1")
        resolver = _make_resolver()
        with patch(
            "subprocess.run",
            return_value=_failed_run(),
        ) as mock_run:
            result = validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/example-repo.git",
                verbose=False,
                auth_resolver=resolver,
            )
        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 2, f"APM_ALLOW_PROTOCOL_FALLBACK should chain, got {urls!r}"
        assert _scheme_of(urls[0]) == "https"
        assert _scheme_of(urls[1]) == "ssh"


class TestPerAttemptVerboseLogging:
    """Verbose mode must surface every attempt's sanitized failure, not just
    the last one. Previously, only the final probe's stderr was logged, which
    masked the real HTTPS failure behind the SSH-fallback timeout."""

    def test_verbose_logs_each_attempt_with_scheme_and_sanitized_stderr(
        self, monkeypatch
    ):
        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "1")  # force 2 attempts
        resolver = _make_resolver()
        verbose_msgs: list[str] = []
        logger = MagicMock()
        logger.verbose = True
        logger.verbose_detail.side_effect = lambda msg: verbose_msgs.append(msg)

        with patch(
            "subprocess.run",
            side_effect=[
                _failed_run("fatal: Authentication failed for HTTPS"),
                _failed_run("ssh: connect to host port 22: Connection timed out"),
            ],
        ):
            validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/example-repo.git",
                verbose=True,
                auth_resolver=resolver,
                logger=logger,
            )

        joined = "\n".join(verbose_msgs)
        # Both attempts must be logged with their scheme so users can diagnose
        # which transport actually failed and why.
        assert "(https)" in joined, f"https attempt missing from log: {joined!r}"
        assert "(ssh)" in joined, f"ssh attempt missing from log: {joined!r}"
        assert "Authentication failed for HTTPS" in joined
        assert "port 22" in joined
