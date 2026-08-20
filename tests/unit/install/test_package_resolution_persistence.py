"""Tests for dependency-list persistence messaging."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from apm_cli.install.package_resolution import (
    get_existing_dep_ref_for_identity,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
)
from apm_cli.models.dependency.reference import DependencyReference


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
