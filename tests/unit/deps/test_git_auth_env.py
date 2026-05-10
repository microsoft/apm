"""Unit tests for ``apm_cli.deps.git_auth_env``.

Cover the three flavours of git env the downloader needs:

1. ``setup_environment`` -- auth-bearing primary env for git ops.
2. ``noninteractive_env`` -- pop-then-conditionally-restore credential-helper
   fence for unauthenticated fallback attempts.
3. ``subprocess_env_dict`` -- sanitized base env merged with auth env for
   cache-layer subprocess calls.

Tagged secure-by-default: silent drift here would pass a wrong env dict to
the git subprocess with no test failing.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.git_auth_env import GitAuthEnvBuilder


class _FakeTokenManager:
    """Mimic GitHubTokenManager.setup_environment() return value."""

    def __init__(self, env: dict | None = None) -> None:
        self._env = env or {}

    def setup_environment(self) -> dict:
        return dict(self._env)


# ---------------------------------------------------------------------------
# setup_environment
# ---------------------------------------------------------------------------


class TestSetupEnvironment:
    def test_pat_path_sets_git_askpass_and_fence_vars(self):
        # Token manager already injected GITHUB_APM_PAT-style env.
        tm = _FakeTokenManager(
            {
                "GITHUB_TOKEN": "ghp_xxx",
                "GIT_CREDENTIAL_USERNAME": "x-access-token",
            }
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SSH_COMMAND", None)
            env = GitAuthEnvBuilder(tm).setup_environment()

        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == "echo"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        # Token-manager-provided keys preserved.
        assert env["GITHUB_TOKEN"] == "ghp_xxx"
        # ConnectTimeout always added.
        assert "ConnectTimeout=30" in env["GIT_SSH_COMMAND"]

    def test_bearer_path_preserves_token_manager_env(self):
        # ADO bearer flow: token_manager publishes Authorization-style
        # signal via env (e.g. GIT_HTTP_EXTRAHEADER on ADO bearer paths).
        tm = _FakeTokenManager(
            {
                "GIT_HTTP_EXTRAHEADER": "Authorization: Bearer aad_jwt_xyz",
            }
        )
        env = GitAuthEnvBuilder(tm).setup_environment()
        assert env["GIT_HTTP_EXTRAHEADER"] == "Authorization: Bearer aad_jwt_xyz"
        # Bearer/PAT distinction is at token_manager layer; the fence still
        # disables interactive prompts.
        assert env["GIT_ASKPASS"] == "echo"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_empty_token_manager_still_produces_sanitized_env(self):
        env = GitAuthEnvBuilder(_FakeTokenManager()).setup_environment()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == "echo"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "GIT_CONFIG_GLOBAL" in env

    def test_existing_ssh_command_preserves_existing_connecttimeout(self):
        tm = _FakeTokenManager()
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -o ConnectTimeout=5"}, clear=False):
            env = GitAuthEnvBuilder(tm).setup_environment()
        # Existing ConnectTimeout NOT overridden (case-insensitive check).
        assert env["GIT_SSH_COMMAND"] == "ssh -o ConnectTimeout=5"

    def test_existing_ssh_command_appends_connecttimeout(self):
        tm = _FakeTokenManager()
        with patch.dict(
            os.environ, {"GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no"}, clear=False
        ):
            env = GitAuthEnvBuilder(tm).setup_environment()
        assert "StrictHostKeyChecking=no" in env["GIT_SSH_COMMAND"]
        assert "ConnectTimeout=30" in env["GIT_SSH_COMMAND"]

    def test_git_config_global_set_to_devnull_on_unix(self):
        if sys.platform == "win32":
            return  # platform-specific; Windows path tested separately
        env = GitAuthEnvBuilder(_FakeTokenManager()).setup_environment()
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


# ---------------------------------------------------------------------------
# noninteractive_env
# ---------------------------------------------------------------------------


class TestNoninteractiveEnv:
    def _base(self) -> dict[str, str]:
        return {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GITHUB_TOKEN": "ghp_xxx",
        }

    def test_default_pops_askpass_and_drops_config_isolation(self):
        # HTTPS/SSH unauthenticated fallback: let user credential helpers
        # resolve naturally (default credential.helper, Keychain, SSH agent).
        env = GitAuthEnvBuilder.noninteractive_env(self._base())
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_ASKPASS" not in env
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # Token left as-is for subprocess to ignore; no leak via env.
        assert env["GITHUB_TOKEN"] == "ghp_xxx"

    def test_preserve_config_isolation_keeps_global_and_nosystem(self):
        env = GitAuthEnvBuilder.noninteractive_env(self._base(), preserve_config_isolation=True)
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        # Askpass still cleared (this is the noninteractive contract).
        assert "GIT_ASKPASS" not in env

    def test_suppress_credential_helpers_sets_full_fence(self):
        # HTTP transport: block credential.helper, askpass, system config.
        env = GitAuthEnvBuilder.noninteractive_env(self._base(), suppress_credential_helpers=True)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == "echo"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""

    def test_default_clears_credential_helper_fence_keys(self):
        base = self._base()
        base["GIT_CONFIG_COUNT"] = "1"
        base["GIT_CONFIG_KEY_0"] = "credential.helper"
        base["GIT_CONFIG_VALUE_0"] = ""
        env = GitAuthEnvBuilder.noninteractive_env(base)
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env


# ---------------------------------------------------------------------------
# subprocess_env_dict
# ---------------------------------------------------------------------------


class TestSubprocessEnvDict:
    def test_merges_auth_env_over_sanitized_base(self):
        # Pre-existing GIT_DIR / GIT_CEILING_DIRECTORIES would bias cache
        # operations; git_subprocess_env() removes them. The merge then
        # overlays auth env on top.
        with patch.dict(
            os.environ,
            {"GIT_DIR": "/some/biased/dir", "GIT_CEILING_DIRECTORIES": "/oops"},
            clear=False,
        ):
            base = {"GITHUB_TOKEN": "ghp_xxx", "GIT_ASKPASS": "echo"}
            env = GitAuthEnvBuilder.subprocess_env_dict(base)
        assert env["GITHUB_TOKEN"] == "ghp_xxx"
        assert env["GIT_ASKPASS"] == "echo"
        # Sanitized base must not propagate ambient GIT_DIR.
        assert "GIT_DIR" not in env

    def test_skips_non_string_values(self):
        # Defensive: dicts with non-string vals don't raise; only strings
        # get merged in.
        base = {"GITHUB_TOKEN": "ghp_xxx", "BAD": 42}  # type: ignore[dict-item]
        env = GitAuthEnvBuilder.subprocess_env_dict(base)
        assert env["GITHUB_TOKEN"] == "ghp_xxx"
        assert "BAD" not in env
