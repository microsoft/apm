"""Hermetic coverage for the live GitLab smoke-test helpers."""

from __future__ import annotations

from pathlib import Path

from tests.integration import test_live_generic_gitlab_install as live_gitlab


def test_env_with_isolated_home_strips_token_vars(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GITLAB_APM_PAT", "glpat-secret")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fallback")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-actions-token")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "actions-runtime-token")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/leaky-gitconfig")
    monkeypatch.setenv("APM_RUN_INTEGRATION_TESTS", "1")

    env = live_gitlab._env_with_isolated_home(tmp_path)

    assert env["HOME"] == str(tmp_path)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["APM_E2E_TESTS"] == "1"
    assert env["APM_RUN_INTEGRATION_TESTS"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert "GITLAB_APM_PAT" not in env
    assert "GITLAB_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "ACTIONS_RUNTIME_TOKEN" not in env
    assert "GIT_CONFIG_GLOBAL" not in env


def test_tail_output_truncates_long_output() -> None:
    text = "a" * (live_gitlab._OUTPUT_TAIL_CHARS + 10)

    result = live_gitlab._tail_output(text)

    assert result.startswith("[truncated to last")
    assert result.endswith("a" * live_gitlab._OUTPUT_TAIL_CHARS)
