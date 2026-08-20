"""Real-git regressions for install preflight auth sanitization."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.install.pipeline import _preflight_auth_check
from apm_cli.models.apm_package import DependencyReference

_SENTINEL = "INHERITED_AUTH_SENTINEL"


@pytest.mark.parametrize(
    "inherited",
    [
        {
            "GIT_CONFIG_PARAMETERS": (f"'http.extraheader=Authorization: Basic {_SENTINEL}'"),
        },
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {_SENTINEL}",
        },
    ],
)
def test_preflight_child_uses_auth_resolver_sanitized_environment(
    inherited: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real git child must not observe inherited authorization channels."""
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    dependency = DependencyReference(
        host="git.example.com",
        repo_url="example/package",
        source="git",
        explicit_scheme="https",
    )
    context = SimpleNamespace(
        deps_to_install=[dependency],
        update_refs=True,
        logger=None,
    )
    real_run = subprocess.run
    observed_envs: list[dict[str, str]] = []

    def run_probe(args, **kwargs):
        env = dict(kwargs["env"])
        observed_envs.append(env)
        git_config = real_run(
            ["git", "config", "--list"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
        assert git_config.returncode == 0, git_config.stderr
        assert _SENTINEL not in git_config.stdout
        return subprocess.CompletedProcess(args, 0, stdout="deadbeef\trefs/heads/main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run_probe)

    _preflight_auth_check(
        context,
        AuthResolver(allow_external_fallback=False),
        verbose=False,
    )

    assert len(observed_envs) == 1
    child_env = observed_envs[0]
    assert "GIT_CONFIG_PARAMETERS" not in child_env
    assert _SENTINEL not in " ".join(child_env.values())
