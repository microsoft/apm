"""Shared project MCP document: entry ownership is per destination file (req-mf-027)."""

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

ECHO = {"name": "echo", "registry": False, "transport": "stdio", "command": "echo"}
WRITTEN = {"type": "stdio", "command": "echo", "args": []}
USER_ENTRY = {"type": "stdio", "command": "user-owned-server"}


class _Project:
    def __init__(self, tmp_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
        self.root = isolated.work_root
        (self.root / ".claude").mkdir(parents=True, exist_ok=True)
        self.env = {**isolated.subprocess_env(), "APM_NON_INTERACTIVE": "1", "COLUMNS": "400"}
        self.runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))

    def manifest(self, targets: list[str], *, servers: bool = True) -> None:
        manifest = {"name": "shared-mcp", "version": "1.0.0", "targets": targets}
        if servers:
            manifest["dependencies"] = {"mcp": [ECHO]}
        (self.root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def install(self, *args: str) -> None:
        result = self.runner.run(("install", "--no-policy", *args), cwd=self.root, env=self.env)
        assert result.returncode == 0, result.stdout + result.stderr

    def servers(self) -> dict:
        return json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]

    def owners(self) -> dict:
        lock = yaml.safe_load((self.root / "apm.lock.yaml").read_text(encoding="utf-8"))
        return lock.get("mcp_target_servers") or {}


@pytest.mark.req("req-mf-027")
def test_deactivating_one_target_keeps_entries_another_active_target_selects(
    tmp_path: Path,
) -> None:
    """`claude` and `copilot` share `.mcp.json`; retiring one never strips the other's entry."""
    assert_spec_contains(
        "**[req-mf-027]** When two or more targets write project-scoped MCP",
        "MUST attribute each entry it wrote in that",
        "NOT remove that entry while another active target that writes the same",
        "file still selects the server, and it MUST treat the entry as owned",
    )
    project = _Project(tmp_path)
    (project.root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mine": USER_ENTRY}}), encoding="utf-8"
    )
    project.manifest(["claude", "copilot"])
    project.install()
    assert project.servers() == {"mine": USER_ENTRY, "echo": WRITTEN}
    assert project.owners() == {"claude": ["echo"], "copilot": ["echo"]}
    # The entry one target wrote is not foreign to the other: frozen replay accepts it.
    project.install("--frozen")

    for remaining in ("copilot", "claude"):
        project.manifest(["claude", "copilot"])
        project.install()
        project.manifest([remaining])
        project.install()
        assert project.servers() == {"mine": USER_ENTRY, "echo": WRITTEN}
        assert project.owners() == {remaining: ["echo"]}

    # Once no active target sharing the file selects the server, it may go.
    project.manifest(["claude", "copilot"], servers=False)
    project.install()
    assert project.servers() == {"mine": USER_ENTRY}
