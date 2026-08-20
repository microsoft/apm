"""Hermetic coverage for the live GitLab smoke-test helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.live_subprocess_environment import (
    LIVE_SUBPROCESS_ENV_DENYLIST,
    OUTPUT_TAIL_CHARS,
    isolated_live_subprocess_env,
    tail_output,
)

pytestmark = pytest.mark.unit


def test_env_with_isolated_home_strips_credentials_and_git_controls(tmp_path: Path) -> None:
    base_env = {
        "PATH": "/usr/bin",
        "APM_RUN_INTEGRATION_TESTS": "1",
        **{name: "secret-or-unsafe" for name in LIVE_SUBPROCESS_ENV_DENYLIST},
        "GITHUB_APM_PAT_EXAMPLE": "prefixed-secret",
    }

    env = isolated_live_subprocess_env(tmp_path, base_env=base_env)

    assert env["HOME"] == str(tmp_path)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["APM_E2E_TESTS"] == "1"
    assert env["APM_RUN_INTEGRATION_TESTS"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert LIVE_SUBPROCESS_ENV_DENYLIST.isdisjoint(env)
    assert "GITHUB_APM_PAT_EXAMPLE" not in env


def test_tail_output_truncates_long_output() -> None:
    text = "a" * (OUTPUT_TAIL_CHARS + 10)

    result = tail_output(text)

    assert result.startswith("[truncated to last")
    assert result.endswith("a" * OUTPUT_TAIL_CHARS)
