"""Policy matrix for Git subprocess credentials."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo

_PLATFORM_TOKENS = {
    "ADO_APM_PAT": "ado-sentinel",
    "GH_TOKEN": "gh-sentinel",
    "GITHUB_APM_PAT": "github-sentinel",
    "GITHUB_TOKEN": "actions-sentinel",
    "GIT_TOKEN": "git-sentinel",
    "GIT_HTTP_EXTRAHEADER": "Authorization: sentinel",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "http.extraheader",
    "GIT_CONFIG_VALUE_0": "Authorization: sentinel",
}


class _TokenManager:
    """Provide a deterministic hardened base without resolving credentials."""

    def setup_environment(self) -> dict[str, str]:
        return {
            **_PLATFORM_TOKENS,
            "GIT_ASKPASS": "echo",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SSH_COMMAND": "ssh -o ConnectTimeout=30 -o BatchMode=yes",
            "HOME": os.environ.get("HOME", ""),
        }


class _RecordingTokenManager(_TokenManager):
    """Record native helper lookups without exposing fixture credentials."""

    def __init__(self) -> None:
        self.credential_envs: list[dict[str, str]] = []

    def get_token_for_purpose(self, _purpose: str) -> None:
        return None

    def resolve_credential_from_gh_cli(self, _host: str) -> None:
        return None

    def resolve_credential_from_git(
        self,
        _host: str,
        port: int | None = None,
        path: str | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        assert port is None
        assert path is None
        assert env is not None
        self.credential_envs.append(env)


def _context(kind: str) -> AuthContext:
    return AuthContext(
        token=None,
        source="none",
        token_type="unknown",
        host_info=HostInfo(
            host=f"{kind}.example.test",
            kind=kind,
            has_public_repos=True,
            api_base="https://example.test/api",
        ),
        git_env={},
    )


@pytest.mark.parametrize(
    ("kind", "remote_url", "expects_helper", "expects_isolation"),
    [
        ("generic", "https://gitea.example.test/org/repo.git", True, False),
        ("generic", "http://gitea.example.test/org/repo.git", False, True),
        ("generic", "git@gitea.example.test:org/repo.git", True, False),
        ("github", "https://github.com/org/repo.git", False, True),
        ("gitlab", "https://gitlab.com/org/repo.git", False, True),
        ("ado", "https://dev.azure.com/org/project/_git/repo", False, True),
        ("ado", "http://dev.azure.com/org/project/_git/repo", False, True),
        ("github", "git@github.com:org/repo.git", True, False),
        ("gitlab", "git@gitlab.com:org/repo.git", True, False),
        ("ado", "git@ssh.dev.azure.com:v3/org/project/repo", True, False),
    ],
)
def test_git_transport_policy_matrix(
    kind: str,
    remote_url: str,
    expects_helper: bool,
    expects_isolation: bool,
) -> None:
    """Only generic HTTPS and SSH retain native Git credential configuration."""
    env = AuthResolver(token_manager=_TokenManager()).git_env_for_remote(_context(kind), remote_url)

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_TOKEN" not in env
    assert "GIT_HTTP_EXTRAHEADER" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GITHUB_APM_PAT" not in env
    assert "GH_TOKEN" not in env
    assert "ADO_APM_PAT" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env

    if expects_helper:
        assert "GIT_ASKPASS" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        assert "GIT_CONFIG_NOSYSTEM" not in env
    else:
        assert env["GIT_ASKPASS"] == "echo"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"

    if kind == "generic" and expects_isolation:
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
    if remote_url.startswith("git@"):
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        assert "ConnectTimeout=30" in env["GIT_SSH_COMMAND"]


@pytest.mark.parametrize(
    ("kind", "remote_url"),
    [
        ("generic", "http://gitea.example.test/org/repo.git"),
        ("github", "http://github.com/org/repo.git"),
        ("gitlab", "http://gitlab.com/org/repo.git"),
        ("ado", "http://dev.azure.com/org/project/_git/repo"),
    ],
)
def test_http_transport_never_receives_resolved_credentials(kind: str, remote_url: str) -> None:
    """Plaintext HTTP suppresses every helper and APM credential channel."""
    context = _context(kind)
    context = AuthContext(
        token="resolved-token",
        source="test",
        token_type="unknown",
        host_info=context.host_info,
        git_env=context.git_env,
    )

    env = AuthResolver(token_manager=_TokenManager()).git_env_for_remote(context, remote_url)

    assert "GIT_TOKEN" not in env
    assert "GIT_HTTP_EXTRAHEADER" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""


@pytest.mark.windows_compat
def test_http_transport_replaces_caller_global_config() -> None:
    """Plaintext HTTP never retains a config file that can inject headers."""

    class _ConfiguredTokenManager(_TokenManager):
        def setup_environment(self) -> dict[str, str]:
            return {**super().setup_environment(), "GIT_CONFIG_GLOBAL": "/configured/gitconfig"}

    env = AuthResolver(token_manager=_ConfiguredTokenManager()).git_env_for_remote(
        _context("generic"),
        "http://gitea.example.test/org/repo.git",
    )

    global_config = Path(env["GIT_CONFIG_GLOBAL"])
    if os.name == "nt":
        assert global_config != Path(os.devnull)
        assert global_config.is_file()
        assert global_config.read_bytes() == b""
    else:
        assert global_config == Path(os.devnull)


def test_https_to_http_url_rewrite_is_rejected_before_git_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native helpers cannot follow an HTTPS remote onto plaintext HTTP."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".gitconfig"
    subprocess.run(
        (
            "git",
            "config",
            "--file",
            str(config),
            "url.http://127.0.0.1:8080/.insteadOf",
            "https://gitea.example.test/",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    with pytest.raises(ValueError, match="rewrite to insecure HTTP"):
        AuthResolver().git_env_for_remote(
            _context("generic"),
            "https://gitea.example.test/org/repo.git",
        )


@pytest.mark.parametrize(
    ("remote_url", "expected_lookups"),
    [
        ("https://gitea.example.test/org/repo.git", 0),
        ("http://gitea.example.test/org/repo.git", 0),
        ("git@gitea.example.test:org/repo.git", 0),
    ],
)
def test_generic_remote_policy_controls_native_helper_lookup(
    remote_url: str, expected_lookups: int
) -> None:
    """Remote resolution leaves native helper invocation to Git itself."""
    token_manager = _RecordingTokenManager()
    resolver = AuthResolver(token_manager=token_manager)

    resolver.resolve_for_remote("gitea.example.test", remote_url)

    assert len(token_manager.credential_envs) == expected_lookups
    if token_manager.credential_envs:
        lookup_env = token_manager.credential_envs[0]
        assert set(_PLATFORM_TOKENS).isdisjoint(lookup_env)
        assert lookup_env["GIT_TERMINAL_PROMPT"] == "0"


def test_generic_plain_resolve_strips_platform_tokens_from_helper_lookup() -> None:
    """Dependency-style resolution never forwards platform tokens to helpers."""
    token_manager = _RecordingTokenManager()

    AuthResolver(token_manager=token_manager).resolve("gitea.example.test")

    assert len(token_manager.credential_envs) == 1
    lookup_env = token_manager.credential_envs[0]
    assert set(_PLATFORM_TOKENS).isdisjoint(lookup_env)
    assert lookup_env["GIT_TERMINAL_PROMPT"] == "0"
