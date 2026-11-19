"""Root project marketplace map (req-mf-028, req-mf-029; Section 4.2.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.marketplace.errors import UndeclaredMarketplaceError
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.project_marketplaces import (
    project_marketplace_scope,
    require_root_declared_marketplace,
)
from apm_cli.marketplace.registry import add_marketplace, get_marketplace_by_name
from apm_cli.marketplace.user_manifest import DeclaredMarketplace
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from tests.spec_conformance._helpers import assert_spec_contains

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "spec-conformance"
    / "manifest"
    / "valid-root-marketplaces.yml"
)


@pytest.fixture
def user_root(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".apm"
    home.mkdir(parents=True)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    clear_apm_yml_cache()
    yield home
    clear_apm_yml_cache()


def _catalog(root: Path, name: str) -> Path:
    path = root / name
    (path / "p" / ".apm" / "instructions").mkdir(parents=True)
    (path / "p" / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    (path / "p" / ".apm" / "instructions" / "p.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nRules.\n', encoding="utf-8"
    )
    (path / "marketplace.json").write_text(
        f'{{"name": "{name}", "plugins": [{{"name": "p", "source": "./p"}}]}}',
        encoding="utf-8",
    )
    return path


@pytest.mark.req("req-mf-028")
def test_root_manifest_map_is_accepted_and_validated(tmp_path) -> None:
    assert_spec_contains(
        "MUST accept a `marketplaces:` list in the root",
        "`source`, carries an unknown key, or repeats a `name`",
    )
    package = APMPackage.from_apm_yml(FIXTURE)
    assert package.marketplaces == (
        DeclaredMarketplace("contoso", "https://github.com/contoso/agent-marketplace", "main"),
    )
    bad = tmp_path / "apm.yml"
    bad.write_text(
        "name: x\nversion: 1.0.0\nmarketplaces:\n"
        "  - {name: a, source: o/r}\n  - {name: A, source: o/s}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"marketplaces\[1\].*duplicate name"):
        APMPackage.from_apm_yml(bad)


@pytest.mark.req("req-mf-028")
def test_declared_name_wins_and_is_never_registered(tmp_path, user_root) -> None:
    assert_spec_contains(
        "never through a same-named catalog registered in user",
        "never persist a declared entry to any",
    )
    declared = _catalog(tmp_path, "declared")
    registered = _catalog(tmp_path, "registered")
    add_marketplace(MarketplaceSource(name="contoso", url=str(registered)))
    before = (user_root / "marketplaces.json").read_bytes()

    with project_marketplace_scope([DeclaredMarketplace("contoso", str(declared))], notice=None):
        assert get_marketplace_by_name("contoso").local_path == str(declared)

    assert (user_root / "marketplaces.json").read_bytes() == before


@pytest.mark.req("req-mf-029")
def test_transitive_reference_must_name_a_root_declared_entry(tmp_path) -> None:
    assert_spec_contains(
        "MUST refuse to install a dependency whose own",
        "whose marketplace name the root list does not declare",
    )
    declared = [DeclaredMarketplace("contoso", str(_catalog(tmp_path, "c")))]
    with project_marketplace_scope(declared, notice=None):
        require_root_declared_marketplace("contoso", "p", required_by="o/r")
        with pytest.raises(UndeclaredMarketplaceError, match="- name: tools"):
            require_root_declared_marketplace("tools", "p", required_by="o/r")


@pytest.mark.req("req-mf-029")
def test_dependency_declared_map_is_refused(tmp_path, user_root, monkeypatch) -> None:
    other = _catalog(tmp_path, "other")
    dep = tmp_path / "dep"
    (dep / ".apm" / "instructions").mkdir(parents=True)
    (dep / "apm.yml").write_text(
        f"name: dep\nversion: 1.0.0\nmarketplaces:\n  - name: x\n    source: {other.as_posix()}\n",
        encoding="utf-8",
    )
    (dep / ".apm" / "instructions" / "d.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nDep.\n', encoding="utf-8"
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: proj\nversion: 1.0.0\ntargets:\n  - copilot\ndependencies:\n  apm:\n    - ../dep\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["install", "--no-policy"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Dependency 'dep' declares 'marketplaces'" in " ".join(result.output.split())
    assert not (project / ".github").exists()
