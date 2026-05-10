"""Tests for generic-host credential-helper env in install.validation.

Regression: ``apm install https://corp-bitbucket.example/...`` on a generic
(non-GitHub, non-ADO) host set ``preserve_config_isolation=True`` because the
flag was wired to ``prefer_web_probe_first`` instead of ``is_insecure``.  This
kept ``GIT_CONFIG_GLOBAL=/dev/null`` and ``GIT_CONFIG_NOSYSTEM=1`` in the
subprocess env, preventing git from reading user-configured credential helpers
(e.g. osxkeychain, credential-store, manager-core) from ``~/.gitconfig``.

After the fix, ``preserve_config_isolation`` uses ``is_insecure`` (matching
every other call site), so config isolation is only enforced for plaintext
HTTP connections where credential leakage is a real risk.  (issue #1013)
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from apm_cli.deps.github_downloader import GitHubPackageDownloader
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


def _ok_run(*args, **kwargs):
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="abc123\trefs/heads/main\n",
        stderr="",
    )


class TestGenericHostCredentialEnv:
    """Validate that _build_noninteractive_git_env receives the correct
    ``preserve_config_isolation`` and ``suppress_credential_helpers`` flags
    for generic hosts, depending on whether the URL is HTTPS or HTTP."""

    def test_https_generic_host_does_not_preserve_config_isolation(self, monkeypatch):
        """HTTPS generic host: preserve_config_isolation=False so that
        user-configured credential helpers in ~/.gitconfig can work."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()

        original_build = GitHubPackageDownloader._build_noninteractive_git_env
        captured_calls: list[dict] = []

        def _spy(self, *, preserve_config_isolation=False, suppress_credential_helpers=False):
            captured_calls.append(
                {
                    "preserve_config_isolation": preserve_config_isolation,
                    "suppress_credential_helpers": suppress_credential_helpers,
                }
            )
            return original_build(
                self,
                preserve_config_isolation=preserve_config_isolation,
                suppress_credential_helpers=suppress_credential_helpers,
            )

        with (
            patch.object(
                GitHubPackageDownloader,
                "_build_noninteractive_git_env",
                _spy,
            ),
            patch("subprocess.run", side_effect=_ok_run),
        ):
            validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/repo.git",
                verbose=False,
                auth_resolver=resolver,
            )

        assert len(captured_calls) == 1, f"expected exactly one call, got {len(captured_calls)}"
        assert captured_calls[0]["preserve_config_isolation"] is False
        assert captured_calls[0]["suppress_credential_helpers"] is False

    def test_http_generic_host_preserves_config_isolation(self, monkeypatch):
        """HTTP (insecure) generic host: preserve_config_isolation=True
        and suppress_credential_helpers=True to prevent credential leakage."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()

        original_build = GitHubPackageDownloader._build_noninteractive_git_env
        captured_calls: list[dict] = []

        def _spy(self, *, preserve_config_isolation=False, suppress_credential_helpers=False):
            captured_calls.append(
                {
                    "preserve_config_isolation": preserve_config_isolation,
                    "suppress_credential_helpers": suppress_credential_helpers,
                }
            )
            return original_build(
                self,
                preserve_config_isolation=preserve_config_isolation,
                suppress_credential_helpers=suppress_credential_helpers,
            )

        with (
            patch.object(
                GitHubPackageDownloader,
                "_build_noninteractive_git_env",
                _spy,
            ),
            patch("subprocess.run", side_effect=_ok_run),
        ):
            validation._validate_package_exists(
                "http://bitbucket.example.internal/scm/team/repo.git",
                verbose=False,
                auth_resolver=resolver,
            )

        assert len(captured_calls) == 1, f"expected exactly one call, got {len(captured_calls)}"
        assert captured_calls[0]["preserve_config_isolation"] is True
        assert captured_calls[0]["suppress_credential_helpers"] is True


class TestGenericHttpsEnvContents:
    """Concrete environment check: for a generic HTTPS host the resulting
    validate_env must NOT contain the config-isolation keys that block
    credential helpers."""

    def test_https_env_allows_credential_helpers(self, monkeypatch):
        """The env produced for a generic HTTPS URL must not contain
        GIT_CONFIG_GLOBAL=/dev/null or GIT_CONFIG_NOSYSTEM=1, because
        these prevent git from reading credential helpers from
        ~/.gitconfig."""
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()

        captured_env: dict[str, str] = {}

        def _capture_env_run(*args, **kwargs):
            env = kwargs.get("env") or {}
            captured_env.update(env)
            return subprocess.CompletedProcess(
                args=args[0] if args else [],
                returncode=0,
                stdout="abc123\trefs/heads/main\n",
                stderr="",
            )

        with patch("subprocess.run", side_effect=_capture_env_run):
            validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/repo.git",
                verbose=False,
                auth_resolver=resolver,
            )

        # The env passed to subprocess.run for the ls-remote probe
        # must not contain config-isolation keys.
        assert captured_env.get("GIT_CONFIG_GLOBAL") != "/dev/null", (
            "GIT_CONFIG_GLOBAL=/dev/null blocks credential helpers"
        )
        assert captured_env.get("GIT_CONFIG_NOSYSTEM") != "1", (
            "GIT_CONFIG_NOSYSTEM=1 blocks system credential helpers"
        )
