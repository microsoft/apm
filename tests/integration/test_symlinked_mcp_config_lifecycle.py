"""Project MCP configs are only merged into regular files inside the project.

When ``.mcp.json``, ``.github/mcp.json``, ``.vscode/mcp.json`` or
``.kiro/settings/mcp.json`` (or its parent directory) is a symlink,
``apm install`` skips that runtime's MCP write with a warning, reports the
runtime as failed, and leaves the link target unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._containment_kit import build_containment_kit, output

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
]


@pytest.mark.parametrize(
    ("target", "config_rel", "seed"),
    [
        ("claude", ".mcp.json", '{"mcpServers": {}}\n'),
        ("copilot", ".github/mcp.json", '{"mcpServers": {}}\n'),
        ("vscode", ".vscode/mcp.json", '{"servers": {}}\n'),
        ("kiro", ".kiro/settings/mcp.json", '{"mcpServers": {}}\n'),
    ],
)
def test_install_skips_linked_project_mcp_config(
    tmp_path: Path, apm_binary_path: Path, target: str, config_rel: str, seed: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest((target,), mcp=True)
    (kit.project / ".claude").mkdir()
    linked = kit.outside / "config.json"
    linked.write_text(seed, encoding="utf-8")
    config = kit.project / config_rel
    config.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(linked, config)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "does not write MCP config through symlinks" in output(result)
    assert linked.read_text(encoding="utf-8") == seed
    assert config.is_symlink()


@pytest.mark.parametrize(
    ("target", "config_rel"),
    [
        ("claude", ".mcp.json"),
        ("copilot", ".github/mcp.json"),
        ("vscode", ".vscode/mcp.json"),
        ("kiro", ".kiro/settings/mcp.json"),
    ],
)
def test_install_writes_regular_project_mcp_config(
    tmp_path: Path, apm_binary_path: Path, target: str, config_rel: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest((target,), mcp=True)
    (kit.project / ".claude").mkdir()
    (kit.project / ".kiro").mkdir()

    result = kit.run("install", "--no-policy")

    assert result.returncode == 0, output(result)
    assert "localsrv" in (kit.project / config_rel).read_text(encoding="utf-8")


def test_install_skips_linked_project_mcp_config_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("kiro",), mcp=True)
    (kit.project / ".kiro").mkdir()
    os.symlink(kit.outside, kit.project / ".kiro/settings", target_is_directory=True)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "does not write MCP config through symlinks" in output(result)
    assert not any(kit.outside.iterdir())
