"""E2E guard for self-defined stdio MCP env placeholder resolution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import toml
import yaml

pytestmark = pytest.mark.xdist_group(name="home_env")


@pytest.fixture
def apm_command() -> str:
    """Return the APM executable used by this integration test."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


def _write_apm_yml(project_dir: Path) -> None:
    config = {
        "name": "mcp-stdio-env-resolution-e2e",
        "version": "0.0.0",
        "dependencies": {
            "apm": [],
            "mcp": [
                {
                    "name": "env-demo",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "example-mcp"],
                    "env": {
                        "APM_STDIO_E2E_TOKEN": "${APM_STDIO_E2E_TOKEN}",
                        "APM_STDIO_E2E_UNSET_TOKEN": "${APM_STDIO_E2E_UNSET_TOKEN}",
                        "LITERAL_VALUE": "literal-value",
                    },
                }
            ],
        },
    }
    (project_dir / "apm.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _run_install(
    apm_command: str, project_dir: Path, runtime: str, verbose: bool
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["APM_NON_INTERACTIVE"] = "1"
    args = [apm_command, "install"]
    if verbose:
        args.append("--verbose")
    args.extend(["--only", "mcp", "--runtime", runtime, "--target", runtime])
    return subprocess.run(
        args,
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _env_block(project_dir: Path, runtime: str) -> dict[str, str]:
    if runtime == "claude":
        config = json.loads((project_dir / ".mcp.json").read_text(encoding="utf-8"))
        return config["mcpServers"]["env-demo"]["env"]
    config = toml.load(project_dir / ".codex" / "config.toml")
    return config["mcp_servers"]["env-demo"]["env"]


@pytest.mark.parametrize(
    ("runtime", "signal_dir"),
    [
        ("claude", ".claude"),
        ("codex", ".codex"),
    ],
)
@pytest.mark.parametrize("verbose", [False, True], ids=["default-output", "verbose-output"])
def test_self_defined_stdio_env_placeholders_resolve_from_process_env_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apm_command: str,
    runtime: str,
    signal_dir: str,
    verbose: bool,
) -> None:
    """Real install writes process-env values for self-defined stdio env."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / signal_dir).mkdir()
    _write_apm_yml(project_dir)

    secret_value = "stdio-e2e-secret-value=canary"
    monkeypatch.setenv("APM_STDIO_E2E_TOKEN", secret_value)
    monkeypatch.delenv("APM_STDIO_E2E_UNSET_TOKEN", raising=False)

    result = _run_install(apm_command, project_dir, runtime, verbose)

    assert result.returncode == 0, (
        f"apm install failed (rc={result.returncode}).\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    env_block = _env_block(project_dir, runtime)
    assert env_block["APM_STDIO_E2E_TOKEN"] == secret_value
    assert env_block["APM_STDIO_E2E_TOKEN"] != "${APM_STDIO_E2E_TOKEN}"
    assert env_block["APM_STDIO_E2E_UNSET_TOKEN"] == "${APM_STDIO_E2E_UNSET_TOKEN}"
    assert env_block["LITERAL_VALUE"] == "literal-value"

    observed_output = f"{result.stdout}\n{result.stderr}"
    assert secret_value not in observed_output
