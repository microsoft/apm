"""Package resolution choices persisted to apm.yml."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.install.package_resolution import (
    persist_dependency_list_if_changed,
)
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml_roundtrip, load_yaml

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"registries": True}})


def test_unchanged_install_preserves_manifest_bytes_and_skips_writer(tmp_path: Path) -> None:
    """A repeated positional install is not an update or a YAML rewrite."""
    from apm_cli.commands.install import _resolve_package_references
    from apm_cli.utils.yaml_io import load_yaml

    manifest = tmp_path / "apm.yml"
    original = (
        b"# Keep this comment and quoting.\nname: test\nversion: '1.0.0'\n"
        b"dependencies:\n  apm:\n    - 'owner/repo' # existing dependency\n"
    )
    manifest.write_bytes(original)
    data = load_yaml(manifest)
    logger = Mock(verbose=False)
    updated = []

    with (
        patch("apm_cli.commands.install._validate_package_exists", return_value=True),
        patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip") as write,
    ):
        valid, invalid, added, _, entries, changed = _resolve_package_references(
            ["owner/repo"],
            data["dependencies"]["apm"],
            {"owner/repo"},
            logger=logger,
            updated_packages_out=updated,
        )
        persist_dependency_list_if_changed(
            dependencies_changed=changed,
            data=data,
            dep_section="dependencies",
            current_deps=data["dependencies"]["apm"],
            apm_yml_path=manifest,
            apm_yml_filename="apm.yml",
            logger=logger,
            rich_error=Mock(),
            sys_exit=Mock(),
        )

    assert valid == [("owner/repo", True)]
    assert invalid == []
    assert added == updated == []
    assert entries == {"owner/repo": "owner/repo"}
    assert changed is False
    logger.validation_pass.assert_called_once_with("owner/repo", True, False, False)
    logger.success.assert_not_called()
    write.assert_not_called()
    assert manifest.read_bytes() == original


def _resolve_and_persist_request(
    manifest: Path, request: str, default_registry: str | None
) -> tuple[DependencyReference, bool]:
    """Exercise real classification, conditional persistence and manifest routing."""
    from apm_cli.commands.install import _check_package_conflicts, _resolve_package_references

    data = load_yaml(manifest)
    deps = data["dependencies"]["apm"]
    with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
        valid, invalid, added, _, _, changed = _resolve_package_references(
            [request, request],
            deps,
            _check_package_conflicts(deps),
            default_registry=default_registry,
        )
    assert invalid == added == []
    assert len(valid) == 2 and all(present for _, present in valid)
    persist_dependency_list_if_changed(
        dependencies_changed=changed,
        data=data,
        dep_section="dependencies",
        current_deps=deps,
        apm_yml_path=manifest,
        apm_yml_filename="apm.yml",
        rich_error=pytest.fail,
        sys_exit=lambda code: pytest.fail(f"unexpected exit {code}"),
    )
    package = APMPackage.from_mapping(
        load_yaml(manifest), package_path=manifest.parent, manifest_path=manifest
    )
    return package.get_apm_dependencies()[0], changed


@pytest.mark.parametrize("default_registry", [None, "internal"])
@pytest.mark.parametrize("explicit_existing", [False, True])
def test_marketplace_explicit_git_routing_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_registry: str | None,
    explicit_existing: bool,
) -> None:
    """An explicit marketplace Git source cannot remain a default-registry shorthand."""
    from apm_cli.marketplace.models import MarketplacePlugin
    from apm_cli.marketplace.resolver import (
        MarketplacePluginResolution,
        _dependency_reference_from_packed_source,
    )

    plugin = MarketplacePlugin(
        name="tool",
        source={"type": "url", "url": "https://github.com/owner/repo", "ref": "1.0.0"},
    )
    dependency = _dependency_reference_from_packed_source(plugin)
    assert dependency is not None
    resolution = MarketplacePluginResolution(
        canonical=dependency.to_canonical(), plugin=plugin, dependency_reference=dependency
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
        lambda *args, **kwargs: resolution,
    )
    entry = (
        {"git": "https://github.com/owner/repo", "ref": "1.0.0"}
        if explicit_existing
        else "owner/repo#1.0.0"
    )
    data = {"name": "test", "version": "1.0.0", "dependencies": {"apm": [entry]}}
    if default_registry:
        data["registries"] = {
            "default": default_registry,
            default_registry: {"url": "https://registry.example.test"},
        }
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(data, manifest)

    loaded, changed = _resolve_and_persist_request(manifest, "tool@catalog", default_registry)

    assert loaded.source != "registry"
    if default_registry:
        assert loaded.source == "git"
    assert changed is (default_registry is not None and not explicit_existing)
    original = manifest.read_bytes()
    _, changed = _resolve_and_persist_request(manifest, "tool@catalog", default_registry)
    assert changed is False
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize("explicit_https", [True, False])
def test_explicit_https_upgrade_preserves_metadata_and_repeats_without_writes(
    tmp_path: Path,
    with_metadata: bool,
    explicit_https: bool,
) -> None:
    """Upgrade only explicit HTTPS requests, retaining pins and package selections."""
    entry = {"git": "http://github.com/owner/repo", "allow_insecure": True}
    if with_metadata:
        entry.update(
            {"ref": "v1", "alias": "kept", "skills": ["beta", "alpha"], "targets": ["copilot"]}
        )
    data = {"name": "test", "version": "1.0.0", "dependencies": {"apm": [entry]}}
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(data, manifest)
    request = "https://github.com/owner/repo" if explicit_https else "owner/repo"

    loaded, changed = _resolve_and_persist_request(manifest, request, None)

    assert urlparse(loaded.to_github_url()).scheme == ("https" if explicit_https else "http")
    assert loaded.source == "git"
    assert changed is explicit_https
    if with_metadata:
        assert loaded.reference == "v1"
        assert loaded.alias == "kept"
        assert sorted(loaded.skill_subset) == ["alpha", "beta"]
        assert loaded.target_subset == ["copilot"]
    if explicit_https:
        assert loaded.allow_insecure is False
    original = manifest.read_bytes()
    _, changed = _resolve_and_persist_request(manifest, request, None)
    assert changed is False
    assert manifest.read_bytes() == original


def _install_into(manifest: Path, packages: list[str], *, dev: bool) -> dict:
    """Run the real add-to-manifest owner (offline) and return the written manifest."""
    from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

    with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
        _validate_and_add_packages_to_apm_yml(packages, dev=dev, manifest_path=manifest)
    return load_yaml(manifest)


_RESTRICTED = {"git": "https://github.com/acme/c.git", "ref": "v1.0.0", "skills": ["review"]}
_REGISTRY = {"id": "acme/c", "version": "1.0.0", "registry": "internal"}


@pytest.mark.parametrize(
    "declared", [_RESTRICTED, _REGISTRY, "acme/c#v1.0.0"], ids=["restricted", "registry", "pin"]
)
def test_install_dev_moves_a_production_declaration_with_its_fields(
    tmp_path: Path, declared: object
) -> None:
    """#3693: ``install --dev X`` moves X's existing declaration, never a bare copy."""
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(
        {
            "name": "p",
            "version": "1.0.0",
            "registries": {"internal": {"url": "https://r.example"}},
            "dependencies": {"apm": [declared, "acme/d"]},
        },
        manifest,
    )

    data = _install_into(manifest, ["acme/c"], dev=True)

    assert data["dependencies"]["apm"] == ["acme/d"]
    assert data["devDependencies"]["apm"] == [declared]


@pytest.mark.parametrize(
    "declared", [_RESTRICTED, _REGISTRY, "acme/c#v1.0.0"], ids=["restricted", "registry", "pin"]
)
def test_plain_install_keeps_a_dev_declaration_and_its_fields(
    tmp_path: Path, declared: object
) -> None:
    """#3693: a bare ``install X`` keeps X in devDependencies, unchanged."""
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(
        {
            "name": "p",
            "version": "1.0.0",
            "registries": {"internal": {"url": "https://r.example"}},
            "dependencies": {"apm": ["acme/d"]},
            "devDependencies": {"apm": ["acme/e", declared]},
        },
        manifest,
    )
    original = manifest.read_bytes()

    data = _install_into(manifest, ["acme/c"], dev=False)

    assert data["dependencies"]["apm"] == ["acme/d"]
    assert data["devDependencies"]["apm"] == ["acme/e", declared]
    assert manifest.read_bytes() == original


def test_explicit_cli_ref_still_updates_the_kept_dev_declaration(tmp_path: Path) -> None:
    """The existing explicit-change rules apply to the declaration where it lives."""
    manifest = tmp_path / "apm.yml"
    dump_yaml_roundtrip(
        {"name": "p", "version": "1.0.0", "devDependencies": {"apm": ["acme/c#v1.0.0"]}},
        manifest,
    )

    data = _install_into(manifest, ["acme/c#v2.0.0"], dev=False)

    assert data["devDependencies"]["apm"] == ["acme/c#v2.0.0"]
    assert "acme/c#v2.0.0" not in (data.get("dependencies") or {}).get("apm", [])
