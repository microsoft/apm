"""Tests for dependency-list persistence messaging."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from apm_cli.install.package_resolution import (
    dependency_sections,
    get_existing_dep_ref_for_identity,
    merge_structured_entry_into_current_deps,
    persist_dependency_list_if_changed,
    update_existing_dependency_entry_if_needed,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


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


def test_dependency_sections_never_creates_the_other_section() -> None:
    data: dict = {}
    dependency_sections(data, dev=False)
    assert data == {"dependencies": {"apm": []}}
