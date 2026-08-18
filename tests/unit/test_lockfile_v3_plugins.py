"""Lockfile v3 migration tests for installed Agent Plugin lifecycle state."""

from __future__ import annotations

import pytest

import apm_cli.deps.lockfile as lockfile_module
from apm_cli.deps.lockfile import (
    InstalledPluginRecord,
    InstalledPluginRecordCodec,
    LockedDependency,
    LockFile,
    LockfileFormatError,
    UnsupportedLockfileVersionError,
)


def _plugin_record() -> InstalledPluginRecord:
    return InstalledPluginRecordCodec.build(
        identity="stable-plugin",
        version="1.2.3",
        source_kind="local",
        source_locator="/packages/stable-plugin",
        resolved_ref=None,
        source_digest=None,
        scope="project",
        components=(),
        prior_record=None,
    )


def _v2_dependency() -> LockedDependency:
    return LockedDependency(
        repo_url="acme/registry-package",
        source="registry",
        version="1.0.0",
        resolved_url="https://registry.example.invalid/package.tar.gz",
        resolved_hash=f"sha256:{'a' * 64}",
    )


def test_v1_and_v2_inputs_parse_and_emit_without_migration() -> None:
    v1 = LockFile.from_yaml("lockfile_version: '1'\ndependencies: []\n")
    v2 = LockFile.from_yaml(
        "lockfile_version: '2'\ndependencies:\n"
        "  - repo_url: acme/registry-package\n"
        "    source: registry\n"
        "    resolved_url: https://registry.example.invalid/package.tar.gz\n"
        f"    resolved_hash: sha256:{'a' * 64}\n"
    )

    assert v1.lockfile_version == "1"
    assert v2.lockfile_version == "2"
    assert "lockfile_version: '1'" in v1.to_yaml()
    assert "lockfile_version: '2'" in v2.to_yaml()


def test_installed_plugins_emit_v3_and_round_trip() -> None:
    record = _plugin_record()
    lock = LockFile(installed_plugins={record.identity: record})

    assert lock.lockfile_version == "3"
    serialized = lock.to_yaml()
    restored = LockFile.from_yaml(serialized)

    assert lock.lockfile_version == "3"
    assert "lockfile_version: '3'" in serialized
    assert restored.lockfile_version == "3"
    assert restored.installed_plugins == {record.identity: record}


@pytest.mark.parametrize("version", ["1", "2"])
def test_legacy_versions_reject_nonempty_installed_plugin_state(version: str) -> None:
    record = _plugin_record()
    serialized = LockFile(installed_plugins={record.identity: record}).to_yaml()
    legacy_version = serialized.replace("lockfile_version: '3'", f"lockfile_version: '{version}'")

    with pytest.raises(
        LockfileFormatError,
        match="Non-empty installed_plugins requires lockfile_version '3'",
    ):
        LockFile.from_yaml(legacy_version)


def test_removing_last_plugin_downgrades_to_dependency_required_version() -> None:
    record = _plugin_record()
    lock = LockFile(installed_plugins={record.identity: record})
    assert "lockfile_version: '3'" in lock.to_yaml()

    lock.replace_installed_plugins({})
    assert lock.lockfile_version == "1"
    assert "lockfile_version: '1'" in lock.to_yaml()

    lock.add_dependency(_v2_dependency())
    lock.replace_installed_plugins({record.identity: record})
    assert lock.lockfile_version == "3"
    lock.replace_installed_plugins({})
    assert lock.lockfile_version == "2"
    assert "lockfile_version: '2'" in lock.to_yaml()


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_empty_plugin_state_preserves_input_version_until_content_driven_emission(
    version: str,
) -> None:
    lock = LockFile.from_yaml(
        f"lockfile_version: '{version}'\ndependencies: []\ninstalled_plugins: []\n"
    )

    assert lock.lockfile_version == version
    serialized = lock.to_yaml()
    assert lock.lockfile_version == "1"
    assert "lockfile_version: '1'" in serialized


def test_v2_only_reader_rejects_v3_instead_of_dropping_plugin_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _plugin_record()
    serialized = LockFile(installed_plugins={record.identity: record}).to_yaml()
    monkeypatch.setattr(
        lockfile_module,
        "SUPPORTED_LOCKFILE_VERSIONS",
        frozenset({"1", "2"}),
    )

    with pytest.raises(UnsupportedLockfileVersionError, match="Unsupported lockfile version '3'"):
        LockFile.from_yaml(serialized)
