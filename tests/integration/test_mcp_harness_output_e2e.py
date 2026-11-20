"""Installed-CLI contract: MCP install output names harnesses and user-wide files (#3994).

A project install for Windsurf writes the user-wide
``~/.codeium/windsurf/mcp_config.json`` (Windsurf has no workspace MCP config),
so both the add and the removal name that file. Harnesses use their product
names ("OpenCode"), never ``str.title()`` of the target id ("Opencode").
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_WINDSURF_CONFIG = "~/.codeium/windsurf/mcp_config.json"


def _project(tmp_path: Path, apm_binary_path: Path, target: str):
    isolated = IsolatedApmEnvironment.create(tmp_path / target, base_env=dict(os.environ))
    env = {**isolated.subprocess_env(), "COLUMNS": "400"}
    root = isolated.work_root / "app"
    root.mkdir(parents=True)
    dump_yaml({"name": "app", "version": "1.0.0", "targets": [target]}, root / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=root, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        return output

    return isolated, root, apm


def test_windsurf_project_install_names_the_user_wide_config(tmp_path, apm_binary_path):
    isolated, root, apm = _project(tmp_path, apm_binary_path, "windsurf")
    (isolated.home / ".codeium" / "windsurf").mkdir(parents=True)

    added = apm("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready")
    assert f"user-wide: {_WINDSURF_CONFIG} (all Windsurf workspaces)" in added

    dump_yaml({"name": "app", "version": "1.0.0", "targets": ["windsurf"]}, root / "apm.yml")
    removed = apm("install", "--no-policy")
    assert f"Removed stale MCP server 'fetch' from {_WINDSURF_CONFIG}" in removed
    assert "user-wide: all Windsurf workspaces" in removed


def test_opencode_install_uses_the_product_name(tmp_path, apm_binary_path):
    _isolated, root, apm = _project(tmp_path, apm_binary_path, "opencode")
    (root / ".opencode").mkdir()

    output = apm("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready")
    assert "fetch -> OpenCode (configured)" in output
    assert "Opencode" not in output
    # One success line per server: the adapter no longer echoes its own.
    assert "Successfully configured MCP server" not in output
    assert "  + fetch\n" not in output
