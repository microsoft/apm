"""Integration: user-manifest ``marketplaces:`` reconciled by ``apm install -g`` (#3006).

Hermetic: the fake user root and the marketplace are local directories, so
the real source parser, probe, fetch and registry run with no network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache


@pytest.fixture
def user_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".apm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("APM_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home / ".apm"))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    project = tmp_path / "project"
    project.mkdir()
    previous = Path.cwd()
    os.chdir(project)
    clear_apm_yml_cache()
    yield home
    clear_apm_yml_cache()
    os.chdir(previous)


def _marketplace_dir(root: Path, name: str, plugin: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "marketplace.json").write_text(
        json.dumps(
            {
                "name": name,
                "plugins": [{"name": plugin, "description": "demo plugin", "source": "./p"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_user_manifest(home: Path, entries: dict[str, Path]) -> None:
    lines = ["name: me", "version: 1.0.0"]
    if entries:
        lines.append("marketplaces:")
        for name, source in entries.items():
            lines += [f"  - name: {name}", f"    source: {source.as_posix()}"]
    (home / ".apm" / "apm.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _registry(home: Path) -> dict[str, dict]:
    data = json.loads((home / ".apm" / "marketplaces.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in data["marketplaces"]}


def _invoke(*args: str):
    clear_apm_yml_cache()
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _flat(output: str) -> str:
    return " ".join(output.split())


def test_install_g_registers_declared_marketplaces_and_search_finds_them(user_root, tmp_path):
    acme = _marketplace_dir(tmp_path, "acme", "lint-skill")
    _write_user_manifest(user_root, {"acme": acme})

    result = _invoke("install", "-g")

    assert result.exit_code == 0, result.output
    assert "Registered marketplace 'acme' (1 plugin)" in _flat(result.output)
    assert _registry(user_root)["acme"]["origin"] == "manifest"

    search = _invoke("search", "lint@acme")
    assert search.exit_code == 0, search.output
    assert "lint-skill" in _flat(search.output)


def test_install_g_keeps_manual_and_removes_only_manifest_entries(user_root, tmp_path):
    acme = _marketplace_dir(tmp_path, "acme", "a")
    tools = _marketplace_dir(tmp_path, "tools", "t")
    manual = _marketplace_dir(tmp_path, "manual", "m")
    assert _invoke("marketplace", "add", str(manual), "--name", "manual").exit_code == 0
    _write_user_manifest(user_root, {"acme": acme, "tools": tools})
    assert _invoke("install", "-g").exit_code == 0

    _write_user_manifest(user_root, {"acme": acme, "manual": tools})
    result = _invoke("install", "-g")

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Removed marketplace 'tools'" in flat
    assert "Kept manual marketplace 'manual'" in flat
    assert "Kept marketplace(s) unchanged: acme" in flat
    registry = _registry(user_root)
    assert sorted(registry) == ["acme", "manual"]
    assert "origin" not in registry["manual"]
    assert registry["manual"]["url"].endswith("/manual")


def test_project_manifest_with_marketplaces_fails_with_user_manifest_hint(user_root):
    Path("apm.yml").write_text(
        "name: proj\nversion: 1.0.0\nmarketplaces:\n  - name: acme\n    source: o/r\n",
        encoding="utf-8",
    )

    result = _invoke("install")

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "Project and package manifests cannot declare marketplaces" in flat
    assert "apm install -g" in flat
    assert not (user_root / ".apm" / "marketplaces.json").exists()
