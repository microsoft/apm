"""Tests for dependency-list persistence messaging."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.install.package_resolution import (
    get_existing_dep_ref_for_identity,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
    update_existing_dependency_entry_if_needed,
)
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml_roundtrip, load_yaml


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"registries": True}})


def test_persist_dependency_list_reports_generic_manifest_update():
    """Manifest rewrites should not claim every change is marketplace-specific."""
    logger = Mock()
    data = {"dependencies": {"apm": []}}
    current_deps = ["danielmeppiel/genesis#v0.4.0"]

    with patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip") as dump_yaml_roundtrip:
        persist_dependency_list_if_changed(
            dependencies_changed=True,
            data=data,
            dep_section="dependencies",
            current_deps=current_deps,
            apm_yml_path="apm.yml",
            apm_yml_filename="apm.yml",
            logger=logger,
            rich_error=Mock(),
            sys_exit=Mock(),
        )

    dump_yaml_roundtrip.assert_called_once_with(data, "apm.yml")
    logger.success.assert_called_once_with("Updated apm.yml dependency entries")


def test_get_existing_dep_ref_for_identity_finds_match():
    current_deps = [{"id": "testorg/demo-pkg", "version": "1.0.0"}]
    ref = get_existing_dep_ref_for_identity(
        current_deps, "testorg/demo-pkg", dependency_reference_cls=DependencyReference
    )
    assert ref is not None
    assert ref.source == "registry"


def test_get_existing_dep_ref_for_identity_no_match():
    current_deps = [{"id": "testorg/other-pkg", "version": "1.0.0"}]
    ref = get_existing_dep_ref_for_identity(
        current_deps, "testorg/demo-pkg", dependency_reference_cls=DependencyReference
    )
    assert ref is None


def test_merge_structured_entry_refuses_to_convert_registry_dep_to_git():
    """Defense-in-depth: refuse to overwrite a registry-sourced entry with a
    git-shaped structured entry (regression: PR #2166 review)."""
    current_deps = [{"id": "testorg/demo-pkg", "version": "1.0.0"}]

    with pytest.raises(ValueError, match="already declared as a registry dependency"):
        merge_structured_entry_into_current_deps(
            current_deps,
            {"git": "testorg/demo-pkg", "ref": "1.0.0", "skills": ["skill-gamma"]},
            "testorg/demo-pkg",
            "testorg/demo-pkg#1.0.0",
            dependency_reference_cls=DependencyReference,
        )
    # Refused before mutation -- original entry untouched.
    assert current_deps == [{"id": "testorg/demo-pkg", "version": "1.0.0"}]


def test_merge_structured_entry_allows_registry_shaped_replacement():
    """A registry-shaped structured entry may still replace an existing one."""
    current_deps = [{"id": "testorg/demo-pkg", "version": "1.0.0"}]

    merge_structured_entry_into_current_deps(
        current_deps,
        {"id": "testorg/demo-pkg", "version": "1.0.0", "skills": ["skill-gamma"]},
        "testorg/demo-pkg",
        "testorg/demo-pkg#1.0.0",
        dependency_reference_cls=DependencyReference,
    )
    assert current_deps == [
        {"id": "testorg/demo-pkg", "version": "1.0.0", "skills": ["skill-gamma"]}
    ]


def test_merge_structured_entry_allows_git_dep_conversion():
    """Non-registry deps are unaffected by the new guard."""
    current_deps = ["owner/repo#main"]

    merge_structured_entry_into_current_deps(
        current_deps,
        {"git": "owner/repo", "ref": "main", "skills": ["skill-gamma"]},
        "owner/repo",
        "owner/repo#main",
        dependency_reference_cls=DependencyReference,
    )
    assert current_deps == [{"git": "owner/repo", "ref": "main", "skills": ["skill-gamma"]}]


@pytest.mark.parametrize(
    ("existing", "requested"),
    [
        ("owner/repo#main", {"git": "owner/repo", "ref": "main"}),
        (
            {"git": "owner/repo", "skills": ["beta", "alpha"], "targets": ["copilot"]},
            {"git": "owner/repo", "targets": ["copilot"], "skills": ["alpha", "beta"]},
        ),
        (
            {"id": "testorg/demo-pkg", "version": "1.0.0", "registry": "internal"},
            {"registry": "internal", "id": "testorg/demo-pkg", "version": "1.0.0"},
        ),
        (
            {"git": "git.epam.com/group/repo", "path": "plugins/tool"},
            {"path": "plugins/tool", "git": "git.epam.com/group/repo"},
        ),
        pytest.param(
            {"git": "owner/repo", "alias": "old"},
            {"alias": "old", "git": "owner/repo"},
            id="same-alias",
        ),
    ],
)
def test_serialization_entry_is_not_manifest_mutation_intent(existing, requested) -> None:
    """Equivalent forms preserve the original entry and report no update."""
    dep_ref = (
        DependencyReference.parse(requested)
        if isinstance(requested, str)
        else DependencyReference.parse_from_dict(requested)
    )
    current_deps = [existing]
    original = deepcopy(current_deps)
    logger = Mock()

    changed = update_existing_dependency_entry_if_needed(
        current_deps,
        already_in_deps=True,
        apm_yml_entries={dep_ref.to_canonical(): requested},
        canonical=dep_ref.to_canonical(),
        dep_ref=dep_ref,
        identity=dep_ref.get_identity(),
        dependency_reference_cls=DependencyReference,
        logger=logger,
    )

    assert changed is False
    assert current_deps == original
    assert current_deps[0] is existing
    logger.verbose_detail.assert_not_called()


@pytest.mark.parametrize(
    ("existing", "requested"),
    [
        ("owner/repo", "owner/repo#v2"),
        (
            {"git": "owner/repo", "skills": ["alpha"]},
            {"git": "owner/repo", "skills": ["alpha", "beta"]},
        ),
        ({"git": "owner/repo", "skills": ["alpha"]}, "owner/repo"),
        (
            {"git": "owner/repo", "targets": ["copilot"], "skills": ["alpha"]},
            {"git": "owner/repo", "targets": ["codex"], "skills": ["alpha"]},
        ),
        (
            {"id": "testorg/demo-pkg", "version": "1.0.0", "registry": "internal"},
            {"id": "testorg/demo-pkg", "version": "2.0.0", "registry": "internal"},
        ),
        (
            "git.epam.com/group/repo/plugins/tool",
            {"git": "git.epam.com/group/repo", "path": "plugins/tool"},
        ),
        (
            {"git": "git.epam.com/group/repo", "path": "plugins/tool"},
            {"git": "git.epam.com/group/repo/plugins", "path": "tool"},
        ),
        pytest.param(
            {"git": "owner/repo", "alias": "old"},
            {"git": "owner/repo", "alias": "new"},
            id="changed-alias",
        ),
    ],
)
def test_effective_manifest_changes_are_still_applied(existing, requested) -> None:
    """Reference, skills, explicit reset, targets and registry version remain meaningful."""
    dep_ref = (
        DependencyReference.parse(requested)
        if isinstance(requested, str)
        else DependencyReference.parse_from_dict(requested)
    )
    current_deps = [existing]

    changed = update_existing_dependency_entry_if_needed(
        current_deps,
        already_in_deps=True,
        apm_yml_entries={dep_ref.to_canonical(): requested},
        canonical=dep_ref.to_canonical(),
        dep_ref=dep_ref,
        identity=dep_ref.get_identity(),
        dependency_reference_cls=DependencyReference,
    )

    assert changed is True
    assert current_deps == [requested]


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
