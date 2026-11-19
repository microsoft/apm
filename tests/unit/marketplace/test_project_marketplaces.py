"""Root project ``marketplaces:`` map owner (#2903 D1/D3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.client import _cache_key
from apm_cli.marketplace.errors import MarketplaceFetchError, UndeclaredMarketplaceError
from apm_cli.marketplace.models import ORIGIN_PROJECT, MarketplaceSource
from apm_cli.marketplace.project_marketplaces import (
    bind_project_marketplaces,
    declared_marketplace_origin,
    project_marketplace_scope,
    require_root_declared_marketplace,
)
from apm_cli.marketplace.registry import get_marketplace_by_name
from apm_cli.marketplace.user_manifest import DeclaredMarketplace
from apm_cli.policy.parser import PolicyValidationError, load_policy
from apm_cli.policy.policy_checks import run_dependency_policy_checks
from apm_cli.policy.schema import ApmPolicy, MarketplacePolicy


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".apm"
    home.mkdir(parents=True)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    return home


def _catalog(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    (path / "marketplace.json").write_text(
        f'{{"name": "{name}", "plugins": [{{"name": "p", "source": "./p"}}]}}',
        encoding="utf-8",
    )
    return path


class TestResolution:
    def test_declared_name_wins_over_registration_with_one_notice(self, tmp_path):
        declared = _catalog(tmp_path, "declared")
        registered = _catalog(tmp_path, "registered")
        registry_mod.add_marketplace(MarketplaceSource(name="acme", url=str(registered)))
        notices: list[str] = []

        with project_marketplace_scope(
            [DeclaredMarketplace("acme", str(declared))], notice=notices.append
        ):
            first = get_marketplace_by_name("ACME")
            second = get_marketplace_by_name("acme")

        assert first.local_path == str(declared)
        assert first.origin == ORIGIN_PROJECT
        assert second is first
        assert len(notices) == 1
        assert "the registered 'acme'" in notices[0]
        # Outside the scope the registration is used again.
        assert get_marketplace_by_name("acme").local_path == str(registered)

    def test_declaration_is_never_written_to_the_registry(self, tmp_path, _isolate_registry):
        declared = _catalog(tmp_path, "declared")
        with project_marketplace_scope([DeclaredMarketplace("acme", str(declared))], notice=None):
            get_marketplace_by_name("acme")
        assert not (_isolate_registry / "marketplaces.json").exists()
        with pytest.raises(ValueError, match="never written to the registry"):
            registry_mod.add_marketplace(
                MarketplaceSource(name="acme", url=str(declared), origin=ORIGIN_PROJECT)
            )

    def test_unreachable_declared_catalog_is_a_fetch_error(self, tmp_path):
        missing = tmp_path / "missing"
        missing.mkdir()
        with (
            project_marketplace_scope([DeclaredMarketplace("acme", str(missing))], notice=None),
            pytest.raises(MarketplaceFetchError, match="marketplaces:"),
        ):
            get_marketplace_by_name("acme")

    def test_user_scope_and_absent_map_bind_nothing(self, tmp_path):
        declared = (DeclaredMarketplace("acme", str(_catalog(tmp_path, "declared"))),)
        for binding in (
            bind_project_marketplaces(None),
            bind_project_marketplaces(declared, user_scope=True),
        ):
            with binding:
                require_root_declared_marketplace("other", "p", required_by="o/r")


class TestIdentity:
    def test_project_cache_key_never_shares_a_same_named_registration(self):
        manual = MarketplaceSource(name="acme", url="https://github.com/o/r")
        project = MarketplaceSource(
            name="acme", url="https://github.com/o/r", origin=ORIGIN_PROJECT
        )
        other = MarketplaceSource(name="acme", url="https://github.com/x/y", origin=ORIGIN_PROJECT)
        assert len({_cache_key(manual), _cache_key(project), _cache_key(other)}) == 3

    def test_persisted_project_origin_degrades_to_manual(self):
        loaded = MarketplaceSource.from_dict(
            {"name": "acme", "url": "https://github.com/o/r", "origin": "project"}
        )
        assert loaded.origin == "manual"

    def test_declared_origin_matches_the_lock_source_url_form(self):
        entry = DeclaredMarketplace("acme", "https://github.com/Contoso/Agent-Marketplace.git")
        assert declared_marketplace_origin(entry) == "https://github.com/contoso/agent-marketplace"
        assert declared_marketplace_origin(DeclaredMarketplace("l", "/abs/catalog")) == ""


class TestTransitiveRule:
    def test_undeclared_name_fails_with_fix_hint(self, tmp_path):
        declared = [DeclaredMarketplace("acme", str(_catalog(tmp_path, "c")))]
        with project_marketplace_scope(declared, notice=None):
            require_root_declared_marketplace("ACME", "p", required_by="o/r")
            with pytest.raises(UndeclaredMarketplaceError) as exc:
                require_root_declared_marketplace("tools", "p", required_by="o/r")
        message = str(exc.value)
        assert "marketplace 'tools' is not declared in the root apm.yml" in message
        assert "- name: tools" in message


class TestMarketplaceAllowPolicy:
    @staticmethod
    def _check(allow, declared):
        policy = ApmPolicy(marketplace=MarketplacePolicy(allow=allow))
        result = run_dependency_policy_checks(
            [], policy=policy, fail_fast=False, declared_marketplaces=declared
        )
        return next((c for c in result.checks if c.name == "marketplace-allow"), None)

    def test_origins_match_host_path_globs(self):
        declared = (
            DeclaredMarketplace("ok", "https://github.com/contoso/agent-marketplace"),
            DeclaredMarketplace("ssh", "git@github.com:contoso/tools.git"),
            DeclaredMarketplace("bad", "https://github.com/evil/catalog"),
            DeclaredMarketplace("local", "/abs/catalog"),
        )
        check = self._check(("https://github.com/contoso/*",), declared)
        assert not check.passed
        assert [s.ref for s in check.subjects] == ["marketplace 'bad'", "marketplace 'local'"]
        assert "github.com/evil/catalog" in check.subjects[0].reason

    def test_no_opinion_or_no_map_skips_the_check(self):
        declared = (DeclaredMarketplace("bad", "https://github.com/evil/catalog"),)
        assert self._check(None, declared) is None
        assert self._check(("github.com/contoso/*",), None) is None
        assert not self._check((), declared).passed

    def test_malformed_allow_list_is_rejected(self, tmp_path):
        policy = tmp_path / "apm-policy.yml"
        policy.write_text(
            "name: p\nmarketplace:\n  allow: github.com/contoso/*\n", encoding="utf-8"
        )
        with pytest.raises(PolicyValidationError, match=r"marketplace\.allow must be a YAML list"):
            load_policy(policy)
