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

    def run(self, *args: str):
        return self.runner.run(args, cwd=self.root, env=self.env)

    def install(self, *args: str) -> None:
        result = self.run("install", "--no-policy", *args)
        assert result.returncode == 0, result.stdout + result.stderr

    def servers(self) -> dict:
        return json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]

    def owners(self) -> dict:
        lock = yaml.safe_load((self.root / "apm.lock.yaml").read_text(encoding="utf-8"))
        return lock.get("mcp_target_servers") or {}


def _shared_project(tmp_path: Path) -> _Project:
    project = _Project(tmp_path)
    (project.root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mine": USER_ENTRY}}), encoding="utf-8"
    )
    return project


@pytest.mark.req("req-mf-027")
def test_deactivating_one_target_keeps_entries_another_active_target_selects(
    tmp_path: Path,
) -> None:
    """`claude` and `copilot` share `.mcp.json`; retiring one never strips the other's entry."""
    assert_spec_contains(
        "**[req-mf-027]** When two or more targets write project-scoped MCP",
        "implementation attributes each entry it wrote in that",
        "example because that target is deactivated, it MUST NOT remove the",
        "selects the server, and it MUST treat the entry as owned for that",
    )
    project = _shared_project(tmp_path)
    project.manifest(["claude", "copilot"])
    project.install()
    assert project.servers() == {"mine": USER_ENTRY, "echo": WRITTEN}
    assert project.owners() == {"claude": ["echo"], "copilot": ["echo"]}
    # (a) The entry one target wrote is not foreign to the other: frozen replay accepts it.
    project.install("--frozen")

    # (b) Retiring one target keeps the entry and hands its claim to the active one.
    project.manifest(["copilot"])
    project.install()
    assert project.servers() == {"mine": USER_ENTRY, "echo": WRITTEN}
    assert project.owners() == {"copilot": ["echo"]}
    # `claude` never wrote this entry for itself: it holds only the inherited claim.
    project.manifest(["claude"])
    project.install()
    assert project.servers() == {"mine": USER_ENTRY, "echo": WRITTEN}
    assert project.owners() == {"claude": ["echo"]}

    # (c) With no active target selecting the server, the inherited claim removes it.
    project.manifest(["claude"], servers=False)
    project.install()
    assert project.servers() == {"mine": USER_ENTRY}


@pytest.mark.req("req-mf-027")
def test_inherited_entry_keeps_the_recorded_write_checks(tmp_path: Path) -> None:
    """An entry edited after another target wrote it is preserved, not deleted."""
    assert_spec_contains(
        "checks it applies to its own entries, including",
        "remaining target from then on, retaining a native write hash",
    )
    project = _shared_project(tmp_path)
    for targets in (["copilot"], ["claude"]):
        project.manifest(targets)
        project.install()
    edited = {**WRITTEN, "args": ["--mine"]}
    path = project.root / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"mine": USER_ENTRY, "echo": edited}}), encoding="utf-8"
    )

    project.manifest(["claude"], servers=False)
    result = project.run("install", "--no-policy")

    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert "Preserved MCP connection(s) echo" in output
    assert project.servers() == {"mine": USER_ENTRY, "echo": edited}
