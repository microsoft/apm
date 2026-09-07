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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.transport_selection import ProtocolPreference
from apm_cli.install import validation
from apm_cli.utils.git_env import GitUrlRewriteProbeError


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
    resolver.resolve_for_remote.return_value = ctx
    resolver.git_env_for_remote.return_value = {
        "GIT_TERMINAL_PROMPT": "0",
    }
    resolver.build_noninteractive_git_env.return_value = {
        "GIT_TERMINAL_PROMPT": "0",
    }
    return resolver


def _failed_run(stderr: str = "ssh: connect to host port 22: Connection timed out"):
    return subprocess.CompletedProcess(
        args=[],
        returncode=128,
        stdout="",
        stderr=stderr,
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

    def test_shorthand_defaults_to_strict_https(self, monkeypatch):
        """No explicit scheme or preference uses the selector's HTTPS default."""
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
        assert len(urls) == 1
        assert _scheme_of(urls[0]) == "https"

    @pytest.mark.parametrize(
        ("preference", "expected_scheme"),
        (
            (ProtocolPreference.SSH, "ssh"),
            (ProtocolPreference.HTTPS, "https"),
        ),
    )
    def test_shorthand_uses_resolved_protocol_preference(
        self,
        monkeypatch,
        preference: ProtocolPreference,
        expected_scheme: str,
    ) -> None:
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch("subprocess.run", return_value=_failed_run()) as mock_run:
            result = validation._validate_package_exists(
                "bitbucket.example.internal/scm/team/example-repo",
                verbose=False,
                auth_resolver=resolver,
                protocol_pref=preference,
                allow_protocol_fallback=False,
            )

        assert result is False
        urls = self._probe_urls(mock_run)
        assert len(urls) == 1
        assert _scheme_of(urls[0]) == expected_scheme

    def test_resolved_fallback_preference_chains_from_selected_protocol(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        with patch("subprocess.run", return_value=_failed_run()) as mock_run:
            result = validation._validate_package_exists(
                "bitbucket.example.internal/scm/team/example-repo",
                verbose=False,
                auth_resolver=resolver,
                protocol_pref=ProtocolPreference.SSH,
                allow_protocol_fallback=True,
            )

        assert result is False
        urls = self._probe_urls(mock_run)
        assert [_scheme_of(url) for url in urls] == ["ssh", "https"]

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

    def test_verbose_logs_each_attempt_with_scheme_and_sanitized_stderr(self, monkeypatch):
        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "1")  # force 2 attempts
        resolver = _make_resolver()
        verbose_msgs: list[str] = []
        logger = MagicMock()
        logger.verbose = True
        logger.verbose_detail.side_effect = lambda msg: verbose_msgs.append(msg)

        with patch(
            "apm_cli.utils.git_env.git_remote_refs",
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


def _write_cli_manifest(root: Path) -> None:
    (root / "apm.yml").write_text(
        "name: transport-test\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n",
        encoding="ascii",
    )


@pytest.mark.parametrize(
    ("args", "environment", "saved_pref", "saved_fallback", "expected_pref", "expected_fallback"),
    (
        (
            ("--ssh", "--allow-protocol-fallback"),
            {},
            None,
            False,
            ProtocolPreference.SSH,
            True,
        ),
        (
            (),
            {"APM_GIT_PROTOCOL": "https", "APM_ALLOW_PROTOCOL_FALLBACK": "1"},
            None,
            False,
            ProtocolPreference.HTTPS,
            True,
        ),
        (
            (),
            {},
            "ssh",
            True,
            ProtocolPreference.SSH,
            True,
        ),
    ),
    ids=("cli-flags", "environment", "saved-config"),
)
def test_positional_cli_threads_resolved_transport_policy_to_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    environment: dict[str, str],
    saved_pref: str | None,
    saved_fallback: bool,
    expected_pref: ProtocolPreference,
    expected_fallback: bool,
) -> None:
    """Positional validation receives the same resolved policy as clone."""
    _write_cli_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    with (
        patch(
            "apm_cli.config.get_apm_protocol_pref",
            return_value=saved_pref or environment.get("APM_GIT_PROTOCOL"),
        ),
        patch(
            "apm_cli.config.get_apm_allow_protocol_fallback",
            return_value=saved_fallback or bool(environment),
        ),
        patch(
            "apm_cli.commands.install._validate_package_exists",
            return_value=False,
        ) as validate,
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", *args, "git.example.test/acme/repo"],
        )

    assert result.exit_code != 0
    assert validate.call_args.kwargs["protocol_pref"] == expected_pref
    assert validate.call_args.kwargs["allow_protocol_fallback"] is expected_fallback


@pytest.mark.parametrize(
    ("probe_error", "expected_text"),
    (
        (False, "different network host"),
        (True, "Unable to verify Git URL rewrite safety"),
    ),
    ids=("unsafe-rewrite", "probe-failure"),
)
def test_positional_cli_preserves_git_rewrite_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: bool,
    expected_text: str,
) -> None:
    """Rewrite failures stay actionable instead of becoming parse fallback."""
    _write_cli_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv(
        "GIT_CONFIG_KEY_0",
        "url.https://mirror.example/.insteadOf",
    )
    monkeypatch.setenv(
        "GIT_CONFIG_VALUE_0",
        "https://git.example.test/",
    )
    load_patch = (
        patch(
            "apm_cli.deps.transport_selection.GitConfigInsteadOfResolver._load_rewrites",
            side_effect=GitUrlRewriteProbeError("fixture probe failure"),
        )
        if probe_error
        else patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    )

    with (
        load_patch,
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "git.example.test/acme/repo"],
        )

    assert result.exit_code != 0
    rendered = " ".join(result.output.split())
    assert expected_text in rendered
    assert "git config --show-origin --get-regexp" in rendered
