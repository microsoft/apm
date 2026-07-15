"""Load-bearing contracts for dependency lock-state consumers."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import fields

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

_LOCKED_DEPENDENCY_VALUES = {
    "repo_url": "apm-org/apm-project/consume-contract",
    "host": "dev.azure.com",
    "host_type": "gitlab",
    "port": 2222,
    "registry_prefix": "registry/git",
    "resolved_commit": "a" * 40,
    "resolved_ref": "main",
    "version": "1.2.3",
    "virtual_path": "skills/alpha",
    "is_virtual": True,
    "depth": 2,
    "resolved_by": "group/parent",
    "package_type": "skill_bundle",
    "deployed_files": [".agents/skills/alpha/SKILL.md"],
    "deployed_file_hashes": {
        ".agents/skills/alpha/SKILL.md": f"sha256:{'b' * 64}",
    },
    "source": "registry",
    "local_path": "../consume-contract",
    "declaring_parent": "group/parent",
    "anchored_local_path": "/workspace/consume-contract",
    "content_hash": f"sha256:{'c' * 64}",
    "is_dev": True,
    "discovered_via": "fixture-marketplace",
    "marketplace_plugin_name": "fixture-plugin",
    "source_url": "https://registry.example.invalid/fixture.json",
    "source_digest": f"sha256:{'d' * 64}",
    "is_insecure": True,
    "allow_insecure": True,
    "skill_subset": ["alpha", "beta"],
    "target_subset": ["copilot"],
    "resolved_url": "https://registry.example.invalid/consume-contract.tgz",
    "resolved_hash": f"sha256:{'e' * 64}",
    "constraint": "^1.0.0",
    "resolved_tag": "v1.2.3",
    "resolved_at": "2026-01-01T00:00:00+00:00",
    "declared_license": "MIT",
    "exec_status": "deployed",
    "name": "consume-contract",
    "ado_organization": "apm-org",
    "ado_project": "apm-project",
    "ado_repo": "consume-contract",
    "_unknown_fields": {"future_consumer_field": {"enabled": True}},
}

_RECONSTRUCTED_LOCK_FIELDS = {
    "repo_url",
    "host",
    "host_type",
    "port",
    "registry_prefix",
    "resolved_ref",
    "version",
    "virtual_path",
    "is_virtual",
    "source",
    "local_path",
    "declaring_parent",
    "anchored_local_path",
    "is_insecure",
    "allow_insecure",
    "skill_subset",
    "target_subset",
    "ado_organization",
    "ado_project",
    "ado_repo",
}

_REFERENCE_TO_LOCK_FIELD = {
    "artifactory_prefix": "registry_prefix",
    "is_local": "source",
}


def _fields_read_by(function: object, variable_name: str) -> set[str]:
    """Return attributes read from one named object in a function body."""
    source = textwrap.dedent(inspect.getsource(function))
    tree = ast.parse(source)
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == variable_name
    }


def _locked_fields_read_by_reconstruction() -> set[str]:
    """Return owner fields consumed by ``to_dependency_ref``."""
    return _fields_read_by(LockedDependency.to_dependency_ref, "self")


def _url_consumer_lock_projection() -> set[str]:
    """Derive lock fields required by the canonical repository URL consumer."""
    reference_fields = {field.name for field in fields(DependencyReference)}
    consumed: set[str] = set()
    pending = [DependencyReference.to_github_url]
    inspected: set[str] = set()
    while pending:
        consumer = pending.pop()
        consumer_name = consumer.__qualname__
        if consumer_name in inspected:
            continue
        inspected.add(consumer_name)
        attributes = _fields_read_by(consumer, "self")
        consumed.update(attributes.intersection(reference_fields))
        for attribute in attributes - reference_fields:
            nested_consumer = getattr(DependencyReference, attribute, None)
            if callable(nested_consumer):
                pending.append(nested_consumer)
    return {_REFERENCE_TO_LOCK_FIELD.get(field_name, field_name) for field_name in consumed}


def test_every_locked_dependency_field_survives_yaml_round_trip() -> None:
    """Every declared lock field must be populated and survive real YAML."""
    declared_fields = {field.name for field in fields(LockedDependency)}
    assert set(_LOCKED_DEPENDENCY_VALUES) == declared_fields

    dependency = LockedDependency(**_LOCKED_DEPENDENCY_VALUES)
    lockfile = LockFile(generated_at="2026-01-01T00:00:00+00:00")
    lockfile.add_dependency(dependency)

    restored = LockFile.from_yaml(lockfile.to_yaml())

    assert restored.get_dependency(dependency.get_unique_key()) == dependency


def test_reconstruction_declares_and_preserves_every_consumed_lock_field() -> None:
    """The lock owner must reconstruct every field its consumer reads."""
    declared_fields = {field.name for field in fields(LockedDependency)}
    projected_fields = _url_consumer_lock_projection()

    assert projected_fields <= declared_fields
    assert projected_fields <= _locked_fields_read_by_reconstruction()
    assert _locked_fields_read_by_reconstruction() == _RECONSTRUCTED_LOCK_FIELDS

    dependency = LockedDependency(
        repo_url="group/consume-contract",
        host="gitlab.example.invalid",
        host_type="gitlab",
        port=2222,
        registry_prefix="registry/git",
        resolved_ref="main",
        version="1.2.3",
        virtual_path="skills/alpha",
        is_virtual=True,
        source="local",
        local_path="../consume-contract",
        declaring_parent="group/parent",
        anchored_local_path="/workspace/consume-contract",
        is_insecure=True,
        allow_insecure=True,
        skill_subset=["beta", "alpha"],
        target_subset=["copilot"],
    )

    reconstructed = dependency.to_dependency_ref()

    assert reconstructed.repo_url == dependency.repo_url
    assert reconstructed.host == dependency.host
    assert reconstructed.host_type == dependency.host_type
    assert reconstructed.port == dependency.port
    assert reconstructed.artifactory_prefix == dependency.registry_prefix
    assert reconstructed.reference == dependency.resolved_ref
    assert reconstructed.virtual_path == dependency.virtual_path
    assert reconstructed.is_virtual is dependency.is_virtual
    assert reconstructed.source == dependency.source
    assert reconstructed.is_local is True
    assert reconstructed.local_path == dependency.local_path
    assert reconstructed.declaring_parent == dependency.declaring_parent
    assert reconstructed.anchored_local_path == dependency.anchored_local_path
    assert reconstructed.is_insecure is dependency.is_insecure
    assert reconstructed.allow_insecure is dependency.allow_insecure
    assert reconstructed.skill_subset == ["alpha", "beta"]
    assert reconstructed.target_subset == ["copilot"]


def test_ado_coordinates_survive_lock_creation_yaml_and_reconstruction() -> None:
    """ADO URL coordinates remain complete across the real lock lifecycle."""
    parsed = DependencyReference.parse(
        "https://dev.azure.com/apm-org/apm-project/_git/consume-contract#v1.0.0"
    )
    locked = LockedDependency.from_dependency_ref(
        parsed,
        resolved_commit="a" * 40,
        depth=1,
        resolved_by=None,
    )

    restored = LockedDependency.from_dict(locked.to_dict())
    reconstructed = restored.to_dependency_ref()

    assert reconstructed.host == "dev.azure.com"
    assert reconstructed.ado_organization == "apm-org"
    assert reconstructed.ado_project == "apm-project"
    assert reconstructed.ado_repo == "consume-contract"
    assert reconstructed.reference == "v1.0.0"
    assert reconstructed.to_github_url() == (
        "https://dev.azure.com/apm-org/apm-project/_git/consume-contract"
    )


def test_legacy_ado_lock_without_coordinates_reconstructs_canonically() -> None:
    """Pre-fix ADO locks remain readable through the reference owner."""
    legacy = LockedDependency(
        repo_url="apm-org/apm-project/consume-contract",
        host="dev.azure.com",
        resolved_commit="a" * 40,
        resolved_ref="v1.0.0",
    )

    reconstructed = legacy.to_dependency_ref()

    assert reconstructed.ado_organization == "apm-org"
    assert reconstructed.ado_project == "apm-project"
    assert reconstructed.ado_repo == "consume-contract"


@pytest.mark.parametrize(
    "missing_field",
    ("ado_organization", "ado_project", "ado_repo"),
)
def test_partial_ado_lock_coordinates_fail_closed(missing_field: str) -> None:
    """Dropping any one persisted ADO field is a rejected partial state."""
    values = {
        "ado_organization": "apm-org",
        "ado_project": "apm-project",
        "ado_repo": "consume-contract",
    }
    values[missing_field] = None
    dependency = LockedDependency(
        repo_url="apm-org/apm-project/consume-contract",
        host="dev.azure.com",
        **values,
    )

    with pytest.raises(ValueError, match="Partial Azure DevOps lock coordinates"):
        dependency.to_dependency_ref()


def test_non_ado_lock_reconstruction_has_no_ado_coordinates() -> None:
    """Generic Git hosts remain unaffected by the ADO projection."""
    parsed = DependencyReference.parse("https://gitlab.example.invalid/group/consume-contract#main")
    locked = LockedDependency.from_dependency_ref(
        parsed,
        resolved_commit="a" * 40,
        depth=1,
        resolved_by=None,
    )

    reconstructed = locked.to_dependency_ref()

    assert reconstructed.ado_organization is None
    assert reconstructed.ado_project is None
    assert reconstructed.ado_repo is None
    assert reconstructed.to_github_url() == (
        "https://gitlab.example.invalid/group/consume-contract"
    )


def test_registry_reconstruction_uses_locked_exact_version() -> None:
    """Registry consumers must use the locked version, not the input range."""
    dependency = LockedDependency(
        repo_url="group/consume-contract",
        source="registry",
        resolved_ref="resolved-range-value",
        version="1.2.3",
    )

    reconstructed = dependency.to_dependency_ref()

    assert reconstructed.source == "registry"
    assert reconstructed.reference == "1.2.3"
