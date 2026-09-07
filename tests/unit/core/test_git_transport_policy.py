"""Policy matrix for Git subprocess credentials."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
from apm_cli.utils.git_env import (
    GitUrlRewriteError,
    get_git_executable,
    git_network_env,
    resolve_git_url_rewrite,
    validate_resolved_git_url_rewrite,
)

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
_AMBIENT_HEADER_TOKEN = "github_pat_" + "Z" * 30


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


def test_native_credential_env_drops_managed_token_and_retains_helper_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation fallback delegates to Git without replaying the managed PAT."""
    config = tmp_path / "gitconfig"
    config.write_text(
        "[credential]\n"
        "\thelper = fixture-helper\n"
        "[http]\n"
        "\textraHeader = Authorization: Basic ambient-sentinel\n",
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    resolver = AuthResolver(token_manager=_TokenManager())

    env = resolver.build_native_git_credential_env(
        _context("gitlab").host_info,
        "https://gitlab.com/org/repo.git",
    )

    assert env["GIT_CONFIG_GLOBAL"] == str(config)
    assert "GIT_ASKPASS" not in env
    assert "GIT_TOKEN" not in env
    assert "GIT_HTTP_EXTRAHEADER" not in env
    assert "GITLAB_APM_PAT" not in env
    entries = [
        (
            env.get(f"GIT_CONFIG_KEY_{index}", ""),
            env.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
    ]
    assert ("http.extraheader", "") in entries
    result = subprocess.run(
        (
            "git",
            "config",
            "--get-urlmatch",
            "http.extraHeader",
            "https://gitlab.com/org/repo.git",
        ),
        check=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
    )
    assert result.stdout == b"\n"


def _urlmatched_headers(env: dict[str, str], remote_url: str, cwd: Path) -> list[str]:
    """Return the effective extraHeader values selected by real Git."""
    result = subprocess.run(
        (
            get_git_executable(),
            "config",
            "--get-urlmatch",
            "http.extraHeader",
            remote_url,
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.stdout.splitlines()


def _ambient_auth_config(path: Path, remote_url: str) -> None:
    """Write URL-scoped stale auth plus a safe header and native helper."""
    path.write_text(
        f'[http "{remote_url}"]\n'
        "\textraHeader = Authorization: Basic ambient-stale\n"
        f"\textraHeader = X-Metadata: {_AMBIENT_HEADER_TOKEN}\n"
        "\textraHeader = X-Trace-Id: safe-value\n"
        "[credential]\n"
        "\thelper = ambient-helper\n",
        encoding="ascii",
    )


def test_public_github_anonymous_fence_beats_url_scoped_ambient_auth(
    tmp_path: Path,
) -> None:
    """Git's URL matcher sees safe headers but no credential on anonymous probes."""
    remote_url = "https://github.com/acme/widgets.git"
    config = tmp_path / "gitconfig"
    _ambient_auth_config(config, remote_url)
    base_env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    anonymous = AuthResolver.build_public_github_anonymous_git_env(base_env=base_env)
    child = git_network_env(remote_url, anonymous)
    headers = _urlmatched_headers(child, remote_url, tmp_path)

    assert len(headers) == 1
    assert headers[0] == "X-Trace-Id: safe-value"
    assert "ambient-stale" not in repr(child)
    assert _AMBIENT_HEADER_TOKEN not in repr(child)
    scoped_values = subprocess.run(
        (
            get_git_executable(),
            "config",
            "--get-all",
            f"http.{remote_url}.extraheader",
        ),
        check=True,
        capture_output=True,
        text=True,
        env=child,
        cwd=tmp_path,
    ).stdout.splitlines()
    assert scoped_values == ["", "X-Trace-Id: safe-value"]
    helpers = subprocess.run(
        (get_git_executable(), "config", "--get-all", "credential.helper"),
        check=False,
        capture_output=True,
        text=True,
        env=child,
        cwd=tmp_path,
    ).stdout.splitlines()
    assert helpers[-1:] == [""]


def test_anonymous_snapshot_preserves_safe_header_from_normal_global_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hardened base still snapshots safe entries from HOME's gitconfig."""
    remote_url = "https://github.com/acme/widgets.git"
    home = tmp_path / "home"
    home.mkdir()
    _ambient_auth_config(home / ".gitconfig", remote_url)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)

    anonymous = AuthResolver.build_public_github_anonymous_git_env()
    child = git_network_env(remote_url, anonymous)

    headers = _urlmatched_headers(child, remote_url, tmp_path)
    assert len(headers) == 1
    assert headers[0] == "X-Trace-Id: safe-value"
    assert "ambient-stale" not in repr(child)
    assert _AMBIENT_HEADER_TOKEN not in repr(child)


@pytest.mark.parametrize(
    ("kind", "host", "remote_path", "scheme", "expected_prefix"),
    (
        ("github", "github.com", "/acme/widgets.git", "basic", "Basic "),
        ("ghe_cloud", "acme.ghe.com", "/acme/widgets.git", "basic", "Basic "),
        ("ghes", "github.acme.test", "/acme/widgets.git", "basic", "Basic "),
        ("gitlab", "gitlab.com", "/acme/widgets.git", "basic", "Basic "),
        ("ado", "dev.azure.com", "/acme/project/_git/widgets", "basic", "Basic "),
        ("ado", "dev.azure.com", "/acme/project/_git/widgets", "bearer", "Bearer "),
    ),
)
def test_managed_fence_selects_only_resolver_header_for_effective_url(
    tmp_path: Path,
    kind: str,
    host: str,
    remote_path: str,
    scheme: str,
    expected_prefix: str,
) -> None:
    """Managed hosts replace URL-scoped ambient auth without dropping safe headers."""
    remote_url = f"https://{host}{remote_path}"
    config = tmp_path / f"{kind}-{scheme}.gitconfig"
    _ambient_auth_config(config, remote_url)

    class _ConfiguredTokenManager(_TokenManager):
        def setup_environment(self) -> dict[str, str]:
            return {
                **super().setup_environment(),
                "GIT_CONFIG_GLOBAL": str(config),
            }

    token = "resolver-selected-token"
    context = AuthContext(
        token=token,
        source="test",
        token_type="unknown",
        host_info=HostInfo(
            host=host,
            kind=kind,
            has_public_repos=kind == "github",
            api_base=f"https://{host}/api",
        ),
        git_env={},
        auth_scheme=scheme,
    )

    managed = AuthResolver(token_manager=_ConfiguredTokenManager()).git_env_for_remote(
        context,
        remote_url,
    )
    child = git_network_env(remote_url, managed)
    headers = _urlmatched_headers(child, remote_url, tmp_path)

    assert len(headers) == 1
    assert headers[0].startswith(f"Authorization: {expected_prefix}")
    assert "ambient-stale" not in repr(child)
    assert _AMBIENT_HEADER_TOKEN not in repr(child)
    scoped_values = subprocess.run(
        (
            get_git_executable(),
            "config",
            "--get-all",
            f"http.{remote_url}.extraheader",
        ),
        check=True,
        capture_output=True,
        text=True,
        env=child,
        cwd=tmp_path,
    ).stdout.splitlines()
    assert scoped_values == [
        "",
        "X-Trace-Id: safe-value",
        headers[0],
    ]
    if scheme == "bearer":
        assert headers[0] == f"Authorization: Bearer {token}"
    else:
        encoded = headers[0].split(" ", 2)[2]
        decoded = base64.b64decode(encoded).decode()
        expected_user = (
            "oauth2" if kind == "gitlab" else ("" if kind == "ado" else "x-access-token")
        )
        assert decoded == f"{expected_user}:{token}"

    sibling_headers = _urlmatched_headers(
        child,
        f"https://{host}/other/repo.git",
        tmp_path,
    )
    assert all(not value.lower().startswith("authorization:") for value in sibling_headers)


def test_generic_https_fence_removes_ambient_header_but_preserves_native_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic HTTPS delegates only through native helpers, never ambient headers."""
    remote_url = "https://git.acme.test/acme/widgets.git"
    config = tmp_path / "generic.gitconfig"
    _ambient_auth_config(config, remote_url)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    resolver = AuthResolver(token_manager=_TokenManager())

    native = resolver.git_env_for_remote(_context("generic"), remote_url)
    child = git_network_env(remote_url, native)

    headers = _urlmatched_headers(child, remote_url, tmp_path)
    assert len(headers) == 1
    assert headers[0] == "X-Trace-Id: safe-value"
    helpers = subprocess.run(
        (get_git_executable(), "config", "--get-all", "credential.helper"),
        check=True,
        capture_output=True,
        text=True,
        env=child,
        cwd=tmp_path,
    ).stdout.splitlines()
    assert helpers == ["ambient-helper"]
    assert "ambient-stale" not in repr(child)
    assert _AMBIENT_HEADER_TOKEN not in repr(child)


def test_managed_header_is_scoped_to_effective_rewritten_url(tmp_path: Path) -> None:
    """A same-origin rewrite receives selected auth only at its effective path."""
    requested = "https://github.com/acme/widgets.git"
    effective = "https://github.com/mirror/widgets.git"
    config = tmp_path / "rewrite.gitconfig"
    config.write_text(
        f'[url "{effective}"]\n'
        f"\tinsteadOf = {requested}\n"
        f'[http "{effective}"]\n'
        "\textraHeader = Authorization: Basic ambient-stale\n"
        "\textraHeader = X-Trace-Id: safe-value\n",
        encoding="ascii",
    )

    class _ConfiguredTokenManager(_TokenManager):
        def setup_environment(self) -> dict[str, str]:
            return {
                **super().setup_environment(),
                "GIT_CONFIG_GLOBAL": str(config),
            }

    token = "rewrite-selected-token"
    context = AuthContext(
        token=token,
        source="test",
        token_type="unknown",
        host_info=HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        ),
        git_env={},
    )
    env = AuthResolver(token_manager=_ConfiguredTokenManager()).git_env_for_remote(
        context,
        requested,
    )

    child = git_network_env(requested, env)
    matched = _urlmatched_headers(child, effective, tmp_path)

    assert len(matched) == 1
    assert matched[0].startswith("Authorization: Basic ")
    assert "ambient-stale" not in repr(child)
    exact_keys = {
        child[f"GIT_CONFIG_KEY_{index}"]
        for index in range(int(child["GIT_CONFIG_COUNT"]))
        if child[f"GIT_CONFIG_VALUE_{index}"] == matched[0]
    }
    assert exact_keys == {f"http.{effective}.extraheader"}
    assert not _urlmatched_headers(child, "https://github.com/other/repo.git", tmp_path)


@pytest.mark.parametrize("reset_scope", ("local", "worktree"))
def test_local_header_reset_is_frozen_after_snapshot_materialization(
    tmp_path: Path,
    reset_scope: str,
) -> None:
    """A local/worktree reset cannot restore a global Authorization header."""
    worktree = tmp_path / "owned-worktree"
    worktree.mkdir()
    subprocess.run(
        ["git", "-C", str(worktree), "init"],
        check=True,
        capture_output=True,
    )
    if reset_scope == "worktree":
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "extensions.worktreeConfig",
                "true",
            ],
            check=True,
            capture_output=True,
        )
    scope_option = f"--{reset_scope}"
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "config",
            scope_option,
            "http.extraHeader",
            "",
        ],
        check=True,
        capture_output=True,
    )
    config = tmp_path / "global-gitconfig"
    config.write_text(
        '[url "https://git.example.com:8443/"]\n'
        "\tinsteadOf = https://git.example.com/\n"
        "[http]\n"
        "\textraHeader = Authorization: Basic sentinel\n",
        encoding="ascii",
    )
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    child = git_network_env(
        "https://git.example.com/acme/repo",
        env,
        worktree=worktree,
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "config",
            scope_option,
            "--unset-all",
            "http.extraHeader",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        [
            get_git_executable(),
            "-C",
            str(worktree),
            "config",
            "--get-urlmatch",
            "http.extraHeader",
            "https://git.example.com:8443/acme/repo",
        ],
        check=True,
        capture_output=True,
        env=child,
    )

    assert result.stdout == b"\n"
    entries = [
        (
            child.get(f"GIT_CONFIG_KEY_{index}", ""),
            child.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(int(child["GIT_CONFIG_COUNT"]))
    ]
    assert ("http.extraheader", "") in entries


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


def test_authenticated_cross_origin_https_rewrite_is_rejected(
    tmp_path: Path,
) -> None:
    """A resolved token cannot follow an insteadOf rule to another host."""

    class _RewriteTokenManager(_TokenManager):
        def setup_environment(self) -> dict[str, str]:
            return {
                **super().setup_environment(),
                "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.https://mirror.example/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://gitlab.com/",
            }

    (tmp_path / "gitconfig").write_text("", encoding="ascii")
    context = AuthContext(
        token="resolved-token",
        source="test",
        token_type="unknown",
        host_info=_context("gitlab").host_info,
        git_env={},
    )

    with pytest.raises(ValueError, match="different HTTPS origin"):
        AuthResolver(token_manager=_RewriteTokenManager()).git_env_for_remote(
            context,
            "https://gitlab.com/org/repo.git",
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
