"""Guard tests for issue #2825: never persist a resolution-staging path."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.utils.staging_guard import (
    STAGING_DIR_NAME,
    StagingPathLeakError,
    assert_no_staging_paths,
)

_STAGED_NAME = re.escape(STAGING_DIR_NAME)
_STAGED_SCRIPT = f"/home/dev/.apm/apm_modules/{STAGING_DIR_NAME}/abc123/replacements/de/start.mjs"


def test_lockfile_write_rejects_staging_paths(tmp_path: Path) -> None:
    lock = LockFile()
    lock.mcp_servers = ["toolsrv"]
    lock.mcp_configs = {"toolsrv": {"command": "node", "args": [_STAGED_SCRIPT]}}
    lockfile_path = tmp_path / "apm.lock.yaml"

    with pytest.raises(StagingPathLeakError, match=_STAGED_NAME):
        lock.write(lockfile_path)

    assert not lockfile_path.exists()


def test_self_defined_server_info_rejects_staging_paths() -> None:
    dependency = MCPDependency(
        name="toolsrv",
        registry=False,
        transport="stdio",
        command="node",
        args=[_STAGED_SCRIPT],
    )

    with pytest.raises(StagingPathLeakError, match=_STAGED_NAME):
        MCPIntegrator._build_self_defined_info(dependency)


def test_self_defined_server_info_accepts_published_paths() -> None:
    dependency = MCPDependency(
        name="toolsrv",
        registry=False,
        transport="stdio",
        command="node",
        args=["/home/dev/.apm/apm_modules/acme/tool/start.mjs"],
    )

    info = MCPIntegrator._build_self_defined_info(dependency)

    assert info["_raw_stdio"]["args"] == ["/home/dev/.apm/apm_modules/acme/tool/start.mjs"]


@pytest.mark.parametrize(
    "payload",
    [
        _STAGED_SCRIPT,
        Path(_STAGED_SCRIPT),
        {"args": [_STAGED_SCRIPT]},
        [{"env": {"TOOL_HOME": _STAGED_SCRIPT}}],
        {_STAGED_SCRIPT: "value"},
    ],
)
def test_guard_walks_nested_payloads(payload: object) -> None:
    with pytest.raises(StagingPathLeakError, match=_STAGED_NAME):
        assert_no_staging_paths(payload, "apm.lock.yaml")


def test_guard_error_names_the_offending_field() -> None:
    payload = {"_raw_stdio": {"command": "node", "args": ["ok", _STAGED_SCRIPT]}}

    with pytest.raises(StagingPathLeakError, match=r"_raw_stdio\.args\[1\] references"):
        assert_no_staging_paths(payload, "MCP client configuration for 'toolsrv'")


def test_guard_allows_clean_payloads() -> None:
    assert_no_staging_paths({"args": ["/home/dev/.apm/apm_modules/acme/tool/start.mjs"]}, "x")
