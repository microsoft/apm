"""Partial MCP result persistence honors the existing frozen lock-write owner.

This component proof exercises the real error-path writer beneath command
admission. It does not claim that a frozen CLI command may write native state.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from apm_cli.core.command_logger import InstallLogger
from apm_cli.deps.lockfile import LockFile, suppress_lockfile_writes
from apm_cli.install.errors import MCPNativeConflictError
from apm_cli.install.mcp.integration import run_mcp_integration
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.yaml_io import load_yaml_str
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.component


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize("frozen_context", [False, True], ids=["ordinary", "frozen-context"])
def test_partial_mcp_conflict_persistence_respects_lock_write_suppression(
    tmp_path: Path, frozen_context: bool
) -> None:
    """A reachable partial-write branch records intent but cannot rewrite a frozen lock."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root
    declaration = {
        "name": "notes",
        "registry": False,
        "transport": "http",
        "url": "http://127.0.0.1:8000/company",
    }
    native_path = project / ".mcp.json"
    native_path.write_text(
        json.dumps(
            {
                "preferences": {"keep": True},
                "mcpServers": {
                    "notes": {"type": "http", "url": "http://127.0.0.1:9/personal"},
                },
            }
        ),
        encoding="ascii",
    )
    native_before = native_path.read_bytes()
    lock_path = project / "apm.lock.yaml"
    lock = LockFile()
    lock.mcp_servers = ["notes"]
    lock.mcp_configs = {"notes": declaration}
    lock.write(lock_path)
    lock_before = lock_path.read_bytes()
    context = suppress_lockfile_writes() if frozen_context else nullcontext([])

    with patch.dict(os.environ, isolated.subprocess_env(), clear=True), context as attempts:
        package = APMPackage.from_mapping(
            {
                "name": "collision-lock-consumer",
                "version": "1.0.0",
                "targets": ["claude", "codex"],
                "dependencies": {"mcp": [declaration]},
            },
            package_path=project,
            create_config=False,
        )
        with pytest.raises(MCPNativeConflictError) as caught:
            run_mcp_integration(
                apm_package=package,
                mcp_deps=package.get_all_mcp_dependencies(),
                apm_modules_path=project / "apm_modules",
                lock_path=lock_path,
                old_mcp_servers={"notes"},
                old_mcp_configs={"notes": declaration},
                old_mcp_provenance={},
                old_mcp_target_servers={},
                project_root=project,
                user_scope=False,
                should_install=True,
                logger=InstallLogger(verbose=False),
                explicit_target=["claude", "codex"],
                no_policy=True,
            )

    assert caught.value.wrote_connections
    assert native_path.read_bytes() == native_before
    codex = tomlkit.parse((project / ".codex/config.toml").read_text(encoding="utf-8"))
    assert dict(codex["mcp_servers"]["notes"]) == {"url": declaration["url"]}
    if frozen_context:
        assert len(attempts) == 1
        assert load_yaml_str(attempts[0])["mcp_target_servers"] == {"codex": ["notes"]}
        assert lock_path.read_bytes() == lock_before
    else:
        assert attempts == []
        assert lock_path.read_bytes() != lock_before
        persisted = LockFile.read(lock_path)
        assert persisted is not None
        assert persisted.mcp_target_servers == {"codex": ["notes"]}
