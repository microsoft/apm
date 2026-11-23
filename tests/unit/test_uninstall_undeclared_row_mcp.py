"""An undeclared, unreachable lock row keeps no MCP server alive on uninstall (#4896)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-post-uninstall-dependency-reachability"),
]


def _package(root: Path, name: str) -> None:
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (root / "apm.yml").write_text(
        f"name: {name}\nversion: 1.0.0\ndependencies:\n  mcp:\n"
        "    - name: shared-srv\n      registry: false\n      transport: stdio\n"
        "      command: echo\n",
        encoding="ascii",
    )
    (instructions / f"{name}.instructions.md").write_text(
        f"---\napplyTo: '**'\n---\n# {name}\n", encoding="ascii"
    )


def _manifest(project: Path, *paths: str) -> None:
    deps = "".join(f"    - path: {path}\n" for path in paths)
    (project / "apm.yml").write_text(
        f"name: app\nversion: 1.0.0\ntargets: [copilot]\ndependencies:\n  apm:\n{deps}",
        encoding="ascii",
    )


def _install_two_sharing_one_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _package(tmp_path / "a", "pkga")
    _package(tmp_path / "b", "pkgb")
    project = tmp_path / "app"
    project.mkdir()
    _manifest(project, "../a", "../b")
    monkeypatch.chdir(project)
    install = CliRunner().invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output
    assert "shared-srv" in _servers(project)
    return project


def _servers(project: Path) -> set[str]:
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8")) or {}
    config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    return set(lock.get("mcp_servers") or []) | set(config.get("mcpServers") or {})


def test_a_hand_removed_package_keeps_no_shared_server_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _install_two_sharing_one_server(tmp_path, monkeypatch)
    _manifest(project, "../b")
    clear_apm_yml_cache()  # a hand edit happens between two processes

    uninstall = CliRunner().invoke(cli, ["uninstall", "../b"])

    assert uninstall.exit_code == 0, uninstall.output
    assert "Removed stale MCP server 'shared-srv'" in uninstall.output
    assert "shared-srv" not in _servers(project)


def test_a_still_declared_package_keeps_its_shared_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _install_two_sharing_one_server(tmp_path, monkeypatch)

    uninstall = CliRunner().invoke(cli, ["uninstall", "../b"])

    assert uninstall.exit_code == 0, uninstall.output
    assert "shared-srv" in _servers(project)
