"""Unit tests for registry-backed ``apm outdated`` checks."""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.client import RegistryError, VersionEntry
from apm_cli.deps.registry.outdated import (
    RegistryOutdatedContext,
    check_registry_locked_dep,
    load_registry_outdated_context,
)
from apm_cli.models.dependency.reference import DependencyReference


def _ctx(
    *,
    repo_url: str = "nadavy/e2e-demo",
    manifest_range: str = "^1.0.0",
    registry_name: str = "test-reg",
    registries: dict[str, str] | None = None,
) -> RegistryOutdatedContext:
    dep = DependencyReference(
        repo_url=repo_url,
        reference=manifest_range,
        source="registry",
        registry_name=registry_name,
    )
    return RegistryOutdatedContext(
        manifest_index={repo_url: dep},
        registries=registries or {registry_name: "https://reg.example.com/apm"},
        default_registry=registry_name,
    )


def _locked(
    *,
    repo_url: str = "nadavy/e2e-demo",
    version: str = "1.0.1",
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        source="registry",
        version=version,
    )


def _fake_client(versions: list[str]):
    fake = MagicMock()
    fake.list_versions.return_value = [
        VersionEntry(version=v, digest=f"sha256:{v}", published_at="2026-01-01T00:00:00Z")
        for v in versions
    ]
    return fake


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"registries": True}},
    )


class TestCheckRegistryLockedDep:
    def test_outdated_when_newer_version_in_range(self):
        ctx = _ctx(manifest_range="^1.0.0")
        locked = _locked(version="1.0.1")
        fake = _fake_client(["1.0.1", "1.1.1"])

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
        )

        assert result.status == "outdated"
        assert result.current == "1.0.1"
        assert result.latest == "1.1.1"
        assert result.source == "registry: test-reg"
        fake.list_versions.assert_called_once_with("nadavy", "e2e-demo")

    def test_up_to_date_when_locked_matches_best(self):
        ctx = _ctx(manifest_range="^1.0.0")
        locked = _locked(version="1.1.1")
        fake = _fake_client(["1.0.1", "1.1.1"])

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
        )

        assert result.status == "up-to-date"
        assert result.latest == "1.1.1"

    def test_unknown_when_feature_disabled(self, monkeypatch):
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {}})
        ctx = _ctx()
        locked = _locked()

        result = check_registry_locked_dep(locked, ctx)

        assert result.status == "unknown"
        assert "feature disabled" in result.source

    def test_unknown_when_not_in_manifest_and_registry_unreachable(self):
        ctx = RegistryOutdatedContext(
            manifest_index={},
            registries={"test-reg": "https://reg.example.com/apm"},
            default_registry="test-reg",
        )
        locked = _locked()
        fake = MagicMock()
        fake.list_versions.side_effect = RegistryError("401 unauthorized")

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
        )

        assert result.status == "unknown"
        assert "(lockfile)" in result.source

    def test_lockfile_only_compares_against_highest_published(self):
        ctx = RegistryOutdatedContext(
            manifest_index={},
            registries={"test-reg": "https://reg.example.com/apm"},
            default_registry="test-reg",
        )
        locked = _locked(version="1.0.1")
        fake = _fake_client(["1.0.1", "1.1.1", "2.0.0"])

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
        )

        assert result.status == "outdated"
        assert result.latest == "2.0.0"
        assert "(lockfile)" in result.source

    def test_unknown_when_invalid_manifest_range(self):
        dep = DependencyReference(
            repo_url="nadavy/e2e-demo",
            reference="main",
            source="registry",
            registry_name="test-reg",
        )
        ctx = RegistryOutdatedContext(
            manifest_index={"nadavy/e2e-demo": dep},
            registries={"test-reg": "https://reg.example.com/apm"},
            default_registry="test-reg",
        )
        locked = _locked()

        result = check_registry_locked_dep(locked, ctx)

        assert result.status == "unknown"
        assert "invalid manifest range" in result.source

    def test_unknown_when_registry_list_versions_fails(self):
        ctx = _ctx()
        locked = _locked()
        fake = MagicMock()
        fake.list_versions.side_effect = RegistryError("401 unauthorized")

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
        )

        assert result.status == "unknown"
        assert result.latest == "-"

    def test_verbose_lists_matching_versions(self):
        ctx = _ctx(manifest_range="^1.0.0")
        locked = _locked(version="1.0.1")
        fake = _fake_client(["1.0.0", "1.0.1", "1.1.1", "2.0.0"])

        result = check_registry_locked_dep(
            locked,
            ctx,
            client_factory=lambda url, auth: fake,
            verbose=True,
        )

        assert result.status == "outdated"
        assert "1.1.1" in result.extra_tags
        assert "2.0.0" not in result.extra_tags


class TestLoadRegistryOutdatedContext:
    def test_loads_manifest_and_registries_from_apm_yml(self, tmp_path, monkeypatch):
        import apm_cli.config as _conf
        from apm_cli.models.apm_package import clear_apm_yml_cache

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {
                "experimental": {"registries": True},
                "registries": {
                    "global-reg": {
                        "url": "https://global.example.com/apm",
                        "default": True,
                    }
                },
            },
        )
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent(
                """
                name: demo
                version: 1.0.0
                registries:
                  project-reg:
                    url: https://project.example.com/apm
                  default: project-reg
                dependencies:
                  apm:
                    - nadavy/e2e-demo#^1.0.0
                """
            ).strip()
            + "\n"
        )

        ctx = load_registry_outdated_context(tmp_path)

        assert "nadavy/e2e-demo" in ctx.manifest_index
        assert ctx.manifest_index["nadavy/e2e-demo"].source == "registry"
        assert ctx.default_registry == "project-reg"
        assert ctx.registries["project-reg"] == "https://project.example.com/apm"
        assert ctx.registries["global-reg"] == "https://global.example.com/apm"

    def test_indexes_transitive_registry_dep_from_installed_package(self, tmp_path, monkeypatch):
        import apm_cli.config as _conf
        from apm_cli.constants import APM_MODULES_DIR
        from apm_cli.models.apm_package import clear_apm_yml_cache

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"registries": True}},
        )
        clear_apm_yml_cache()

        (tmp_path / "apm.yml").write_text(
            textwrap.dedent(
                """
                name: consumer
                version: 1.0.0
                registries:
                  test-reg:
                    url: https://reg.example.com/apm
                  default: test-reg
                dependencies:
                  apm:
                    - microsoft/batch-bug-shepherd#^0.1.0
                """
            ).strip()
            + "\n"
        )

        parent_dir = tmp_path / APM_MODULES_DIR / "microsoft" / "batch-bug-shepherd"
        parent_dir.mkdir(parents=True)
        (parent_dir / "apm.yml").write_text(
            textwrap.dedent(
                """
                name: batch-bug-shepherd
                version: 0.1.0
                dependencies:
                  apm:
                    - microsoft/apm-review-panel#^0.1.0
                """
            ).strip()
            + "\n"
        )

        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="microsoft/apm-review-panel",
                source="registry",
                version="0.1.1",
                depth=2,
                resolved_by="microsoft/batch-bug-shepherd",
            )
        )

        ctx = load_registry_outdated_context(tmp_path, lockfile)

        assert "microsoft/apm-review-panel" in ctx.manifest_index
        assert ctx.manifest_index["microsoft/apm-review-panel"].reference == "^0.1.0"
