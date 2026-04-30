"""End-to-end regression guard: `apm install --verbose` must not leak tokens.

PR #764 fixed a leak at src/apm_cli/install/validation.py where git ls-remote
stderr could echo the embedded PAT in clone URLs. These tests force an auth /
404 failure path with a recognizable bogus canary token and assert that the
canary substring NEVER appears in stdout or stderr, even with --verbose.

Allowed: the redacted form (e.g. `***`, `[REDACTED]`, `x-access-token:***@`).
Forbidden: the literal canary substring in any form.

These tests intentionally use a bogus token, so they do not require a real
GITHUB_TOKEN to be configured in CI.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CANARY = "github_pat_BOGUS_REDACTION_CANARY_DO_NOT_LEAK"
CANARY_CORE = "BOGUS_REDACTION_CANARY_DO_NOT_LEAK"


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project with .github/ for target detection."""
    project_dir = tmp_path / "verbose-redaction-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


def _bogus_env():
    """Build a subprocess env that exposes ONLY the bogus canary token."""
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = CANARY
    env["GITHUB_APM_PAT"] = CANARY
    env.pop("GITHUB_APM_PAT_OVERRIDE", None)
    # Prevent gh CLI / askpass helpers from injecting the real developer token.
    env.pop("GH_TOKEN", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_apm_with_env(apm_command, args, cwd, env, timeout=60):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _write_apm_yml(project_dir, packages):
    config = {
        "name": "verbose-redaction-test",
        "version": "1.0.0",
        "dependencies": {"apm": packages, "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _assert_no_canary(result):
    combined = (result.stdout or "") + (result.stderr or "")
    assert CANARY_CORE not in combined, (
        "Token canary leaked in apm install --verbose output!\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )


def _assert_install_failed(result):
    """Confirm we exercised an error path (either non-zero exit or error marker)."""
    combined = (result.stdout or "") + (result.stderr or "")
    failed = (
        result.returncode != 0
        or "Installation failed" in combined
        or "Failed to download" in combined
        or "Authentication failed" in combined
    )
    assert failed, (
        "Expected install to hit an error path, but it appeared to succeed.\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


class TestVerboseInstallTokenRedaction:
    """Regression guard for PR #764 -- verbose install must redact tokens."""

    def test_verbose_install_does_not_leak_token_on_404_repo(self, temp_project, apm_command):
        """API-probe path: nonexistent shorthand repo ref, auth fails."""
        _write_apm_yml(
            temp_project,
            ["microsoft/this-repo-definitely-does-not-exist-xyz123"],
        )
        result = _run_apm_with_env(
            apm_command,
            ["install", "--verbose"],
            temp_project,
            _bogus_env(),
        )
        _assert_install_failed(result)
        _assert_no_canary(result)

    def test_verbose_install_does_not_leak_token_in_url_form(self, temp_project, apm_command):
        """URL-probe path: explicit git+https URL, auth fails."""
        _write_apm_yml(
            temp_project,
            [{"git": "https://github.com/microsoft/this-also-does-not-exist-xyz789.git"}],
        )
        result = _run_apm_with_env(
            apm_command,
            ["install", "--verbose"],
            temp_project,
            _bogus_env(),
        )
        _assert_install_failed(result)
        _assert_no_canary(result)
