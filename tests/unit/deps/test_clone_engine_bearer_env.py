"""Focused contracts for CloneEngine's ADO bearer-retry env construction (#2368)."""

from __future__ import annotations

import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.transport_selection import TransportAttempt, TransportPlan
from apm_cli.models.dependency.reference import DependencyReference


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
    assert bearer_env["GIT_CONFIG_COUNT"] == "2"
    assert bearer_env["GIT_CONFIG_KEY_0"] == "safe.bareRepository"
    assert bearer_env["GIT_CONFIG_VALUE_0"] == "explicit"
    assert bearer_env["GIT_CONFIG_KEY_1"] == "http.extraheader"
    assert bearer_env["GIT_CONFIG_VALUE_1"] == "Authorization: Bearer fake-bearer-token"
    # The retry must not mutate the host's shared base env.
    assert host.git_env["GIT_CONFIG_COUNT"] == "1"
