"""TLS failure guidance surfaced through the install CLI."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from apm_cli.install import validation

pytestmark = pytest.mark.component


class TestTlsFailureGuidanceThroughCli:
    """#2035: the real ``apm install`` failure path renders owner guidance.

    Trust selection runs through ``configure_tls_trust`` with synthetic
    settings; the GitHub probe fails with a controlled ``SSLError``. No network
    connection, SSL injection, or CA-file read occurs.
    """

    _IGNORED_REMEDIES = (
        "installed in the OS trust store",
        "set APM_EXTRA_CA_BUNDLE to a readable PEM bundle",
    )

    @pytest.fixture
    def tls_failure_cli(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        import apm_cli.core.tls_trust as tls
        from apm_cli.cli import cli
        from apm_cli.core.auth import AuthResolver

        monkeypatch.setattr(tls, "_LAST_TLS_STATUS", tls._LAST_TLS_STATUS)
        monkeypatch.setattr(tls, "_LAST_TLS_SELECTION", tls._LAST_TLS_SELECTION)
        monkeypatch.setattr(
            AuthResolver,
            "try_with_fallback",
            lambda self, host, operation, **kwargs: operation(None, {}),
        )
        monkeypatch.setattr(
            validation,
            "_github_api_get",
            MagicMock(side_effect=requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")),
        )
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\ndependencies:\n  apm: []\n  mcp: []\n", encoding="utf-8"
        )

        def _run(trust_env: dict[str, str], *extra_args: str):
            tls.configure_tls_trust(env=dict(trust_env))
            result = CliRunner().invoke(cli, ["install", "octocat/hello-world", *extra_args])
            return result, " ".join(result.output.split())

        return _run

    @pytest.mark.parametrize("verbose", [False, True], ids=["normal", "verbose"])
    @pytest.mark.parametrize(
        ("trust_env", "expected"),
        [
            (
                {"APM_DISABLE_TRUSTSTORE": "1"},
                ("APM_DISABLE_TRUSTSTORE is set", "unset APM_DISABLE_TRUSTSTORE"),
            ),
            (
                {"REQUESTS_CA_BUNDLE": "/replacement/requests.pem"},
                ("REQUESTS_CA_BUNDLE is set and replaces", "unset REQUESTS_CA_BUNDLE"),
            ),
            (
                {"CURL_CA_BUNDLE": "/replacement/curl.pem"},
                ("CURL_CA_BUNDLE is set and replaces", "unset CURL_CA_BUNDLE"),
            ),
            (
                {
                    "REQUESTS_CA_BUNDLE": "/replacement/requests.pem",
                    "APM_DISABLE_TRUSTSTORE": "1",
                    "APM_EXTRA_CA_BUNDLE": "/ignored/extra.pem",
                },
                (
                    "REQUESTS_CA_BUNDLE is set and replaces",
                    "If you unset REQUESTS_CA_BUNDLE, APM_DISABLE_TRUSTSTORE still applies",
                ),
            ),
        ],
        ids=["opt-out", "requests", "curl", "replacement-opt-out-ignored-extra"],
    )
    def test_selected_mode_guidance_and_failed_exit(
        self, tls_failure_cli, trust_env, expected, verbose
    ):
        result, output = tls_failure_cli(trust_env, *(["--verbose"] if verbose else []))

        assert result.exit_code == 1, result.output
        assert "TLS verification failed --" in output
        for fragment in expected:
            assert fragment in output
        for remedy in self._IGNORED_REMEDIES:
            assert remedy not in output
        assert ("underlying error from github.com" in output) is verbose

    @pytest.mark.parametrize("verbose", [False, True], ids=["normal", "verbose"])
    def test_default_os_store_guidance_is_preserved(self, tls_failure_cli, monkeypatch, verbose):
        import sys
        import types

        fake = types.ModuleType("truststore")
        fake.inject_into_ssl = lambda: None  # type: ignore[attr-defined]
        fake.SSLContext = object  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "truststore", fake)

        result, output = tls_failure_cli({}, *(["--verbose"] if verbose else []))

        assert result.exit_code == 1, result.output
        assert "APM uses the system trust store by default" in output
        for remedy in self._IGNORED_REMEDIES:
            assert remedy in output
