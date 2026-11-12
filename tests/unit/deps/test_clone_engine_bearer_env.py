"""Focused contracts for CloneEngine's ADO bearer-retry env construction (#2368)."""

from __future__ import annotations

import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo
from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan
from apm_cli.models.dependency.reference import DependencyReference
from tests.utils.git_credential_sentinel import (
    credential_helper_trap_env,
    exercise_credential_helper,
)


def _ado_host(git_env: dict) -> MagicMock:
    """Build the downloader-context slice CloneEngine.execute needs."""
    host = MagicMock()
    host.git_env = git_env
    host._allow_fallback = False
    host._fallback_port_warned = set()
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="https-token", use_token=True)],
        strict=False,
    )
    host._resolve_dep_token.return_value = "pat-token"
    ctx = MagicMock()
    ctx.token = "pat-token"
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    ctx.host_info.kind = "ado"
    host._resolve_dep_auth_ctx.return_value = ctx
    resolver_impl = AuthResolver()
    host.auth_resolver.execute_with_bearer_fallback.side_effect = (
        resolver_impl.execute_with_bearer_fallback
    )
    host.auth_resolver.build_ado_bearer_git_env.side_effect = resolver_impl.build_ado_bearer_git_env
    host.auth_resolver.git_env_for_context.side_effect = AuthResolver.git_env_for_context
    host.auth_resolver._build_git_env.side_effect = AuthResolver._build_git_env
    host._build_repo_url = MagicMock(
        side_effect=lambda *a, **kw: (
            "https://bearer-url/o/r" if kw.get("auth_scheme") == "bearer" else "https://pat-url/o/r"
        )
    )
    host._sanitize_git_error = MagicMock(side_effect=lambda s: s)
    return host


def test_bearer_retry_sets_header_preserving_retained_git_config(tmp_path: Path) -> None:
    """#2368: the bearer retry env must append the Authorization header at the
    next free GIT_CONFIG index instead of resetting the count and clobbering
    retained hardening entries (safe.bareRepository etc.) from the base env."""
    host = _ado_host(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "explicit",
        }
    )
    dep = DependencyReference.parse("dev.azure.com/org/proj/_git/repo/skills/X#main")
    assert dep.is_azure_devops(), "fixture sanity: dep must be ADO"

    envs_seen: list[dict] = []

    def clone_action(url: str, env: dict, target: Path) -> None:
        envs_seen.append(dict(env))
        if "pat-url" in url:
            raise sp.CalledProcessError(128, ["git"], stderr=b"Authentication failed")

    bearer_provider = MagicMock()
    bearer_provider.is_available.return_value = True
    bearer_provider.get_bearer_token.return_value = "fake-bearer-token"

    with patch(
        "apm_cli.core.azure_cli.get_bearer_provider",
        return_value=bearer_provider,
    ):
        CloneEngine(host).execute(
            "org/proj/_git/repo",
            tmp_path / "dst",
            dep_ref=dep,
            clone_action=clone_action,
        )

    assert len(envs_seen) == 2, "expected PAT attempt then bearer retry"
    bearer_env = envs_seen[-1]
    assert bearer_env["GIT_CONFIG_COUNT"] == "3"
    assert bearer_env["GIT_CONFIG_KEY_0"] == "safe.bareRepository"
    assert bearer_env["GIT_CONFIG_VALUE_0"] == "explicit"
    assert bearer_env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert bearer_env["GIT_CONFIG_VALUE_1"] == ""
    assert bearer_env["GIT_CONFIG_KEY_2"] == "http.extraheader"
    assert bearer_env["GIT_CONFIG_VALUE_2"] == "Authorization: Bearer fake-bearer-token"
    # The retry must not mutate the host's shared base env.
    assert host.git_env["GIT_CONFIG_COUNT"] == "1"


def test_configured_rewrite_executes_requested_url_once(tmp_path: Path) -> None:
    """Git, not the selector, applies the resolved rewrite to the argv URL."""
    host = _ado_host({})
    requested = "https://github.com/owner/repo"
    effective = "file:///fixture/owner/repo"
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[
            TransportAttempt(
                scheme="file",
                label="Git URL rewrite (file)",
                use_token=False,
                requested_url=requested,
                effective_url=effective,
            )
        ],
        strict=True,
    )
    host._resolve_dep_token.return_value = None
    host._resolve_dep_auth_ctx.return_value = None
    host.auth_resolver.uses_public_github_anonymous_first.return_value = False
    host._build_noninteractive_git_env.return_value = {}
    urls: list[str] = []

    CloneEngine(host).execute(
        "owner/repo",
        tmp_path / "dst",
        dep_ref=DependencyReference.parse("owner/repo"),
        clone_action=lambda url, _env, _target: urls.append(url),
    )

    assert urls == [requested]


def test_github_token_uses_header_not_clone_url(tmp_path: Path) -> None:
    """Resolved GitHub credentials stay out of argv and repository metadata."""
    host = _ado_host({})
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="https-token", use_token=True)],
        strict=True,
    )
    context = host._resolve_dep_auth_ctx.return_value
    context.host_info.kind = "github"
    host.auth_resolver.git_env_for_remote.side_effect = lambda ctx, _url: (
        AuthResolver.git_env_for_context(ctx, base_env=host.git_env)
    )
    host._build_repo_url.side_effect = lambda *_args, **kwargs: (
        f"https://{kwargs['token']}@github.com/owner/repo"
        if kwargs.get("token")
        else "https://github.com/owner/repo"
    )
    seen: list[tuple[str, dict[str, str]]] = []

    CloneEngine(host).execute(
        "owner/repo",
        tmp_path / "dst",
        dep_ref=DependencyReference.parse("owner/repo"),
        clone_action=lambda url, env, _target: seen.append((url, env)),
    )

    assert seen[0][0] == "https://github.com/owner/repo"
    assert "GIT_TOKEN" not in seen[0][1]
    assert any(
        key.startswith("GIT_CONFIG_KEY_") and value == "http.extraheader"
        for key, value in seen[0][1].items()
    )


def test_gitlab_rewrite_uses_effective_transport_credential_policy(tmp_path: Path) -> None:
    """A GitLab rewrite to file transport cannot retain its HTTPS header."""
    host = _ado_host({})
    requested = "https://gitlab.com/owner/repo.git"
    effective = "file:///fixture/owner/repo.git"
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[
            TransportAttempt(
                scheme="file",
                label="Git URL rewrite (file)",
                use_token=False,
                requested_url=requested,
                effective_url=effective,
            )
        ],
        strict=True,
    )
    context = host._resolve_dep_auth_ctx.return_value
    context.host_info.kind = "gitlab"
    host.auth_resolver.resolve_for_remote.return_value = context
    host.auth_resolver.git_env_for_remote.return_value = {"CLEAN": "1"}
    seen_envs: list[dict[str, str]] = []

    CloneEngine(host).execute(
        "owner/repo",
        tmp_path / "dst",
        dep_ref=DependencyReference.parse("https://gitlab.com/owner/repo.git"),
        clone_action=lambda _url, env, _target: seen_envs.append(env),
    )

    host.auth_resolver.git_env_for_remote.assert_called_with(context, effective)
    assert seen_envs == [{"CLEAN": "1"}]


def test_tokenless_ado_clone_never_invokes_native_credential_helper(tmp_path: Path) -> None:
    """CloneEngine routes a tokenless ADO attempt through the resolver fence."""
    base_env, marker = credential_helper_trap_env(tmp_path)
    resolver = AuthResolver()
    dep = DependencyReference.parse("dev.azure.com/org/project/_git/repo#main")
    context = AuthContext(
        token=None,
        source="none",
        token_type="unknown",
        host_info=HostInfo(
            host="dev.azure.com",
            kind="ado",
            has_public_repos=False,
            api_base="https://dev.azure.com",
        ),
        git_env={},
    )
    host = _ado_host(base_env)
    host.auth_resolver = resolver
    host._resolve_dep_token.return_value = None
    host._resolve_dep_auth_ctx.return_value = context
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="plain HTTPS", use_token=False)],
        strict=True,
    )
    host._build_repo_url.return_value = "https://dev.azure.com/org/project/_git/repo"

    def clone_action(_url: str, env: dict[str, str], _target: Path) -> None:
        exercise_credential_helper(
            env,
            host="dev.azure.com",
            path="org/project/_git/repo",
        )

    CloneEngine(host).execute(
        dep.repo_url,
        tmp_path / "clone",
        dep_ref=dep,
        clone_action=clone_action,
    )

    assert not marker.exists()
