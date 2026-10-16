"""Codex MCP repair preserves the existing frozen-admission contract."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import tomlkit

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("user_scope", [False, True])
def test_frozen_mcp_drift_precedes_codex_legacy_repair(
    tmp_path: Path,
    transport: str,
    user_scope: bool,
) -> None:
    """A frozen declaration mismatch cannot repair owned native configuration."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["CODEX_HOME"] = str(isolated.home / "codex-native")
    project = isolated.config_root if user_scope else isolated.work_root
    declaration = (
        "      command: echo\n      args: [original]\n"
        if transport == "stdio"
        else "      url: https://example.test/original\n"
    )
    manifest = project / "apm.yml"
    manifest.write_text(
        "name: frozen-codex\nversion: 1.0.0\ntargets: [codex]\n"
        "dependencies:\n  mcp:\n"
        "    - name: managed\n      registry: false\n"
        f"      transport: {transport}\n{declaration}",
        encoding="ascii",
    )
    args = ("install", "--only", "mcp", "--target", "codex")
    if user_scope:
        args += ("--global",)
    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
    installed = runner.run(args, cwd=isolated.work_root, env=environment)
    assert installed.returncode == 0, installed.stdout + installed.stderr

    config = (Path(environment["CODEX_HOME"]) if user_scope else project / ".codex") / (
        "config.toml"
    )
    native = tomlkit.parse(config.read_text(encoding="utf-8"))
    native["mcp_servers"]["managed"]["id"] = ""
    config.write_text(tomlkit.dumps(native), encoding="utf-8")
    manifest.write_text(
        manifest.read_text(encoding="ascii").replace("original", "changed"), encoding="ascii"
    )
    lockfile = project / "apm.lock.yaml"
    before_config, before_lock, before_manifest = (
        path.read_bytes() for path in (config, lockfile, manifest)
    )

    frozen = runner.run((*args, "--frozen"), cwd=isolated.work_root, env=environment)

    assert frozen.returncode != 0
    assert "out of sync" in frozen.stdout + frozen.stderr
    assert config.read_bytes() == before_config
    assert lockfile.read_bytes() == before_lock
    assert manifest.read_bytes() == before_manifest

    repaired = runner.run(args, cwd=isolated.work_root, env=environment)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert "Repaired 1 Codex MCP entry" in repaired.stdout
    assert "id" not in tomlkit.parse(config.read_text(encoding="utf-8"))["mcp_servers"]["managed"]
