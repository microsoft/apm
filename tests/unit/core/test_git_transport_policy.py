"""Policy matrix for Git subprocess credentials."""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
from apm_cli.utils.git_env import (
    GitUrlRewriteError,
    resolve_git_url_rewrite,
    validate_resolved_git_url_rewrite,
)

pytestmark = pytest.mark.unit

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
    "remote_url",
    (
        "ssh://git@gitlab.com/group/repo.git",
        "git@gitlab.com:group/repo.git",
        "file:///local/mirror.git",
        "http://gitlab.com/group/repo.git",
    ),
)
def test_gitlab_non_https_resolution_never_probes_https_credentials(remote_url: str) -> None:
    """GitLab honors the auth owner's no-native-lookup transport policy."""
    manager = _RecordingTokenManager()
    resolver = AuthResolver(token_manager=manager)

    context = resolver.resolve_for_remote("gitlab.com", remote_url, "group")

    assert context.token is None
    assert manager.credential_envs == []


def test_gitlab_https_resolution_retains_native_credential_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted HTTPS attempt still resolves credentials for its declared port."""
    manager = _RecordingTokenManager()
    lookup = Mock(return_value="helper-token")
    monkeypatch.setattr(manager, "resolve_credential_from_git", lookup)
    resolver = AuthResolver(token_manager=manager)

    context = resolver.resolve_for_remote(
        "gitlab.com", "https://gitlab.com:8443/group/repo.git", "group", port=8443
    )

    assert context.token == "helper-token"
    lookup.assert_called_once_with("gitlab.com", port=8443)


@pytest.mark.parametrize(
    "effective_url",
    (
        "https://mirror.example/acme/repo",
        "ssh://git@mirror.example/acme/repo",
        "git@mirror.example:acme/repo",
        "ssh:///acme/repo",
    ),
)
def test_resolved_rewrite_rejects_cross_host_network_targets(effective_url: str) -> None:
    """Pure policy rejects explicit URL, SCP, and hostless SSH targets."""
    with pytest.raises(GitUrlRewriteError, match="different network host"):
        validate_resolved_git_url_rewrite(
            "https://git.example.com/acme/repo",
            effective_url,
            has_authorization=False,
        )


@pytest.mark.parametrize(
    "effective_url",
    (
        "ssh://git@git.example.com/acme/repo",
        "git@git.example.com:acme/repo",
    ),
)
def test_resolved_rewrite_allows_same_host_protocol_change(effective_url: str) -> None:
    """Pure policy preserves legitimate HTTPS-to-SSH rewrites."""
    validate_resolved_git_url_rewrite(
        "https://git.example.com/acme/repo",
        effective_url,
        has_authorization=False,
    )


def test_resolve_rewrite_uses_longest_matching_prefix() -> None:
    """Pure resolution mirrors Git insteadOf longest-match behavior."""
    effective_url = resolve_git_url_rewrite(
        "https://git.example.com/acme/repo",
        (
            ("https://mirror.example/", "https://git.example.com/"),
            ("ssh://git@git.example.com/", "https://git.example.com/acme/"),
        ),
    )

    assert effective_url is not None
    parsed = urlsplit(effective_url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "ssh",
        "git.example.com",
        "/repo",
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
        ("ado", "git@ssh.dev.azure.com:v3/org/project/repo", False, True),
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
        # HTTPS ADO children get no askpass on POSIX, so a 401 fails with
        # "terminal prompts disabled" instead of echoing prompt text as Basic.
        ado_https_posix = (
            kind == "ado" and remote_url.startswith("https://") and sys.platform != "win32"
        )
        assert env["GIT_ASKPASS"] == ("" if ado_https_posix else "echo")
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
