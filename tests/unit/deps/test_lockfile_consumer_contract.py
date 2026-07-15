"""Load-bearing contracts for dependency lock-state consumers."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import fields

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.unit

_LOCKED_DEPENDENCY_VALUES = {
    "repo_url": "group/consume-contract",
    "host": "gitlab.example.invalid",
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
}


def _locked_fields_read_by_reconstruction() -> set[str]:
    """Return owner fields consumed by ``to_dependency_ref``."""
    source = textwrap.dedent(inspect.getsource(LockedDependency.to_dependency_ref))
    tree = ast.parse(source)
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


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
