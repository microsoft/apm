"""Portable ``${CLAUDE_PLUGIN_ROOT}`` render and legacy-lock equivalence (#3825)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.deps.plugin_root_render import legacy_absolute_equivalent
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency

_AUTHORED = {
    "name": "db",
    "registry": False,
    "transport": "stdio",
    "command": "node",
    "args": ["${CLAUDE_PLUGIN_ROOT}/server.js", "--data=${CLAUDE_PLUGIN_ROOT}/data"],
}


def _expanded(root: str) -> dict:
    return {
        **_AUTHORED,
        "args": [f"{root}/server.js", f"--data={root}/data"],
    }


@pytest.fixture
def package_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj" / "apm_modules" / "acme" / "synth"
    root.mkdir(parents=True)
    return root


def test_legacy_absolute_lock_is_equivalent_here_and_in_another_clone(
    tmp_path: Path, package_root: Path
) -> None:
    project = tmp_path / "proj"
    here = str(package_root.resolve())
    for stored_root in (here, "/ci/other clone/apm_modules/acme/synth"):
        assert legacy_absolute_equivalent(
            _AUTHORED, _expanded(stored_root), plugin_root=here, project_root=project
        )


@pytest.mark.parametrize(
    "stored",
    [
        _expanded("/elsewhere/apm_modules/acme/other"),
        _expanded("relative/apm_modules/acme/synth"),
        {**_expanded("/x/apm_modules/acme/synth"), "command": "python"},
        {
            **_AUTHORED,
            "args": [
                "/a/apm_modules/acme/synth/server.js",
                "--data=/b/apm_modules/acme/synth/data",
            ],
        },
    ],
)
def test_legacy_equivalence_rejects_any_other_change(
    tmp_path: Path, package_root: Path, stored: dict
) -> None:
    assert not legacy_absolute_equivalent(
        _AUTHORED,
        stored,
        plugin_root=str(package_root.resolve()),
        project_root=tmp_path / "proj",
    )


@pytest.mark.parametrize(
    ("adapter", "user_scope", "prefix"),
    [
        (ClaudeClientAdapter, False, "${CLAUDE_PROJECT_DIR:-.}/apm_modules/acme/synth"),
        (CursorClientAdapter, False, "${workspaceFolder}/apm_modules/acme/synth"),
        (CodexClientAdapter, False, None),
        (ClaudeClientAdapter, True, None),
    ],
)
def test_each_target_renders_its_documented_root(
    tmp_path: Path, package_root: Path, adapter, user_scope: bool, prefix: str | None
) -> None:
    dependency = MCPDependency.from_dict(_AUTHORED)
    dependency.plugin_root = str(package_root.resolve())
    client = adapter(project_root=tmp_path / "proj", user_scope=user_scope)

    rendered = client.render_server_config(MCPIntegrator._build_self_defined_info(dependency))

    root = prefix or str(package_root.resolve())
    assert rendered["args"] == [f"{root}/server.js", f"--data={root}/data"]
