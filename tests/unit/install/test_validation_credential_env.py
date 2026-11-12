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

import os
import subprocess
from unittest.mock import MagicMock, patch

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
    """Validate that generic hosts route through remote-aware auth policy."""

    def test_https_generic_host_uses_remote_policy(self, monkeypatch):
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        resolver.git_env_for_remote.return_value = {"GIT_TERMINAL_PROMPT": "0"}

        with (
            patch("subprocess.run", side_effect=_ok_run),
        ):
            validation._validate_package_exists(
                "https://bitbucket.example.internal/scm/team/repo.git",
                verbose=False,
                auth_resolver=resolver,
            )

        resolver.git_env_for_remote.assert_called_with(
            resolver.resolve_for_remote.return_value,
            "https://bitbucket.example.internal/scm/team/repo.git",
        )

    def test_http_generic_host_uses_remote_policy(self, monkeypatch):
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        resolver.git_env_for_remote.return_value = {
            "GIT_ASKPASS": "echo",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with patch("subprocess.run", side_effect=_ok_run):
            validation._validate_package_exists(
                "http://bitbucket.example.internal/scm/team/repo.git",
                verbose=False,
                auth_resolver=resolver,
            )

        resolver.git_env_for_remote.assert_called_with(
            resolver.resolve_for_remote.return_value,
            "http://bitbucket.example.internal/scm/team/repo.git",
        )


class TestGenericHttpsEnvContents:
    """Concrete environment check for immutable generic helper snapshots."""

    def test_https_env_materializes_credential_helper(self, monkeypatch):
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        resolver = _make_resolver()
        resolver.git_env_for_remote.return_value = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "fixture-helper",
            "GIT_TERMINAL_PROMPT": "0",
        }

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

        assert captured_env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert captured_env["GIT_CONFIG_NOSYSTEM"] == "1"
        entries = {
            (
                captured_env.get(f"GIT_CONFIG_KEY_{index}", ""),
                captured_env.get(f"GIT_CONFIG_VALUE_{index}", ""),
            )
            for index in range(int(captured_env.get("GIT_CONFIG_COUNT", "0")))
        }
        assert ("credential.helper", "fixture-helper") in entries
