"""Project-scoped Claude Code MCP configuration keeps environment references."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from tests.spec_conformance._helpers import assert_spec_contains
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e

SECRET = "req-mf-026-secret-sentinel"
OTHER_SECRET = "req-mf-026-other-sentinel"

DECLARED = [
    {
        "name": "refs-stdio",
        "registry": False,
        "transport": "stdio",
        "command": "${REQ_CMD:-uvx}",
        "args": ["refs-server", "--token=${REQ_TOKEN}"],
        "env": {"TOKEN": "${env:REQ_TOKEN}", "MODE": "${REQ_MODE:-safe}", "LOG_LEVEL": "debug"},
    },
    {
        "name": "refs-http",
        "registry": False,
        "transport": "http",
        "url": "https://mcp.example.invalid/mcp?key=${REQ_TOKEN}",
        "headers": {"Authorization": "Bearer ${REQ_TOKEN}"},
    },
]

EXPECTED = {
    "refs-stdio": {
        "type": "stdio",
        "command": "${REQ_CMD:-uvx}",
        "args": ["refs-server", "--token=${REQ_TOKEN}"],
        "env": {"TOKEN": "${REQ_TOKEN}", "MODE": "${REQ_MODE:-safe}", "LOG_LEVEL": "debug"},
    },
    "refs-http": {
        "type": "http",
        "url": "https://mcp.example.invalid/mcp?key=${REQ_TOKEN}",
        "headers": {"Authorization": "Bearer ${REQ_TOKEN}"},
    },
}


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    environment = isolated.subprocess_env()
    environment["REQ_TOKEN"] = SECRET
    environment["APM_NON_INTERACTIVE"] = "1"
    for name in ("REQ_CMD", "REQ_MODE"):
        environment.pop(name, None)
    manifest = {
        "name": "claude-references",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": DECLARED},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return project, environment


def _install(project: Path, environment: dict[str, str]) -> tuple[int, str]:
    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
    result = runner.run(
        ("install", "--only", "mcp", "--no-policy", "--target", "claude"),
        cwd=project,
        env=environment,
    )
    # Join the CLI's terminal-width wrapping back into single spaces.
    return result.returncode, " ".join((result.stdout + result.stderr).split())


def _servers(project: Path) -> dict:
    return json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def _write_values_over_references(project: Path, value: str) -> None:
    path = project / ".mcp.json"
    path.write_text(path.read_text(encoding="utf-8").replace("${REQ_TOKEN}", value), "utf-8")


@pytest.mark.req("req-mf-026")
def test_project_config_keeps_references_instead_of_values(tmp_path: Path) -> None:
    """Every field keeps the native reference; static and defaulted values stay as declared."""
    assert_spec_contains(
        "project-scoped Claude Code `.mcp.json`",
        "MUST NOT write the variable's resolved value",
        "never their values",
    )
    project, environment = _project(tmp_path)

    returncode, output = _install(project, environment)

    assert returncode == 0, output
    assert _servers(project) == EXPECTED
    for text in ((project / ".mcp.json").read_text(), (project / "apm.lock.yaml").read_text()):
        assert SECRET not in text
    assert SECRET not in output


@pytest.mark.req("req-mf-026")
def test_reinstall_replaces_values_the_consumer_wrote(tmp_path: Path) -> None:
    """An entry that is the expansion of the declared references is rewritten."""
    assert_spec_contains("native write hash the consumer recorded for it")
    project, environment = _project(tmp_path)
    assert _install(project, environment)[0] == 0
    _write_values_over_references(project, SECRET)

    returncode, output = _install(project, environment)

    assert returncode == 0, output
    assert _servers(project) == EXPECTED
    assert "Affected: REQ_TOKEN" in output
    assert SECRET not in output


@pytest.mark.req("req-mf-026")
def test_reinstall_keeps_values_it_cannot_show_it_wrote(tmp_path: Path) -> None:
    """Other values are left in place, the install fails, and only field names are shown."""
    assert_spec_contains("MUST leave the entry unchanged")
    project, environment = _project(tmp_path)
    assert _install(project, environment)[0] == 0
    _write_values_over_references(project, OTHER_SECRET)
    before = (project / ".mcp.json").read_bytes()

    returncode, output = _install(project, environment)

    assert returncode != 0
    assert (project / ".mcp.json").read_bytes() == before
    assert "Fields args[1], env.TOKEN hold values" in output
    assert SECRET not in output
    assert OTHER_SECRET not in output
