"""Lifecycle: a root project ``marketplaces:`` map resolves installs (#2903 D1/D3).

Hermetic: catalogs and packages are local directories, so the real manifest
parser, resolver, catalog fetch and lockfile writer run with no network.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-marketplace-project-map"),
]


def _use_user_root(monkeypatch, home: Path) -> None:
    (home / ".apm").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("APM_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home / ".apm"))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    _use_user_root(monkeypatch, tmp_path / "home")
    previous = Path.cwd()
    clear_apm_yml_cache()
    yield tmp_path
    clear_apm_yml_cache()
    os.chdir(previous)


def _package(path: Path, name: str, body: str, extra: str = "") -> Path:
    (path / ".apm" / "instructions").mkdir(parents=True)
    (path / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n{extra}", encoding="utf-8")
    (path / ".apm" / "instructions" / f"{name}.instructions.md").write_text(
        f'---\napplyTo: "**"\n---\n{body}\n', encoding="utf-8"
    )
    return path


def _catalog(root: Path, name: str, body: str) -> Path:
    path = root / name
    _package(path / "p", "lint-skill", body)
    (path / "marketplace.json").write_text(
        json.dumps({"name": name, "plugins": [{"name": "lint-skill", "source": "./p"}]}),
        encoding="utf-8",
    )
    return path


def _project(path: Path, catalog: Path, dependency: str) -> Path:
    path.mkdir(parents=True)
    (path / "apm.yml").write_text(
        "name: proj\nversion: 1.0.0\ntargets:\n  - copilot\n"
        f"marketplaces:\n  - name: acme\n    source: {catalog.as_posix()}\n"
        f"dependencies:\n  apm:\n    - {dependency}\n",
        encoding="utf-8",
    )
    return path


def _invoke(project: Path, *args: str):
    os.chdir(project)
    clear_apm_yml_cache()
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _flat(output: str) -> str:
    return " ".join(output.split())


_MAP_DEP = "{ name: lint-skill, marketplace: acme }"
_DEPLOYED = Path(".github/instructions/lint-skill.instructions.md")


def test_cold_clone_and_ci_install_through_the_map_without_registration(workspace):
    catalog = _catalog(workspace, "acme", "Catalog rules.")
    project = _project(workspace / "proj", catalog, _MAP_DEP)

    result = _invoke(project, "install")

    assert result.exit_code == 0, result.output
    assert "Catalog rules." in (project / _DEPLOYED).read_text(encoding="utf-8")
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "discovered_via: acme" in lock
    assert "marketplace_plugin_name: lint-skill" in lock
    # The map is never written to the user's marketplace registry.
    assert not (workspace / "home" / ".apm" / "marketplaces.json").exists()

    # CI: a fresh checkout with the committed lock and no registration.
    # (A local catalog records no commit, so --frozen cannot replay it.)
    shutil.rmtree(project / "apm_modules")
    shutil.rmtree(project / ".github")
    replay = _invoke(project, "install")

    assert replay.exit_code == 0, replay.output
    assert (project / _DEPLOYED).is_file()
    assert (project / "apm.lock.yaml").read_text(encoding="utf-8") == lock
    assert not (workspace / "home" / ".apm" / "marketplaces.json").exists()


def test_two_machines_with_different_registrations_lock_identical_bytes(workspace, monkeypatch):
    catalog = _catalog(workspace, "acme", "Catalog rules.")
    other = _catalog(workspace, "other", "Other rules.")
    third = _catalog(workspace, "third", "Third rules.")
    results = []
    for machine, registered in (("a", other), ("b", third)):
        _use_user_root(monkeypatch, workspace / f"home-{machine}")
        project = _project(workspace / machine / "proj", catalog, _MAP_DEP)
        added = _invoke(project, "marketplace", "add", str(registered), "--name", "acme")
        assert added.exit_code == 0, added.output

        result = _invoke(project, "install")

        assert result.exit_code == 0, result.output
        assert (
            f"Using marketplace 'acme' from apm.yml ({catalog.as_posix()}); "
            f"the registered 'acme' ({registered.as_posix()}) is not used in this project."
            in _flat(result.output)
        )
        assert _flat(result.output).count("Using marketplace 'acme' from apm.yml") == 1
        results.append(
            (
                (project / "apm.lock.yaml").read_bytes(),
                (project / _DEPLOYED).read_bytes(),
            )
        )

    assert results[0] == results[1]
    assert b"Catalog rules." in results[0][1]


def test_dependency_declared_marketplaces_map_is_refused(workspace):
    catalog = _catalog(workspace, "acme", "Catalog rules.")
    other = _catalog(workspace, "other", "Other rules.")
    _package(
        workspace / "evil",
        "evil",
        "Evil.",
        extra=f"marketplaces:\n  - name: acme\n    source: {other.as_posix()}\n",
    )
    project = _project(workspace / "proj", catalog, "../evil")

    result = _invoke(project, "install")

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "Dependency 'evil' declares 'marketplaces'" in flat
    assert "only the root project's apm.yml may declare" in flat
    assert not (project / ".github").exists()


def test_transitive_dependency_with_undeclared_marketplace_fails_with_fix(workspace):
    catalog = _catalog(workspace, "acme", "Catalog rules.")
    _package(
        workspace / "mid",
        "mid",
        "Mid.",
        extra="dependencies:\n  apm:\n    - { name: lint-skill, marketplace: tools }\n",
    )
    project = _project(workspace / "proj", catalog, "../mid")

    result = _invoke(project, "install")

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "marketplace 'tools' is not declared in the root apm.yml" in flat
    assert "marketplaces: - name: tools source: <catalog repository URL>" in flat


def test_transitive_dependency_resolves_through_a_root_declared_name(workspace):
    catalog = _catalog(workspace, "acme", "Catalog rules.")
    _package(workspace / "mid", "mid", "Mid.", extra=f"dependencies:\n  apm:\n    - {_MAP_DEP}\n")
    project = _project(workspace / "proj", catalog, "../mid")

    result = _invoke(project, "install")

    assert result.exit_code == 0, result.output
    assert "Catalog rules." in (project / _DEPLOYED).read_text(encoding="utf-8")
