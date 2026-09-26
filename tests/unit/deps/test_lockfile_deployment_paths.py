"""Tests for lockfile-owned deployment path mutations."""

from dataclasses import replace
from pathlib import Path

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import LocatorKind
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.deployed_paths import deployed_path_entry
from apm_cli.integration.targets import KNOWN_TARGETS


def test_rename_local_deployed_path_moves_path_and_hash_without_duplicates() -> None:
    lockfile = LockFile(
        local_deployed_files=["old.md", "new.md", "old.md"],
        local_deployed_file_hashes={"old.md": "sha256:old"},
    )

    lockfile.rename_local_deployed_path("old.md", "new.md")

    assert lockfile.local_deployed_files == ["new.md"]
    assert lockfile.local_deployed_file_hashes == {"new.md": "sha256:old"}


def test_rename_local_deployed_path_is_noop_when_old_path_is_absent() -> None:
    lockfile = LockFile(
        local_deployed_files=["kept.md"],
        local_deployed_file_hashes={"old.md": "sha256:orphan"},
    )

    lockfile.rename_local_deployed_path("missing.md", "new.md")

    assert lockfile.local_deployed_files == ["kept.md"]
    assert lockfile.local_deployed_file_hashes == {"old.md": "sha256:orphan"}


def test_rename_local_deployed_path_invalidates_canonical_projection() -> None:
    lockfile = LockFile(
        local_deployed_files=["old.md"],
        local_deployed_file_hashes={"old.md": "sha256:old"},
    )
    lockfile.deployment_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    lockfile._deployments_present = True

    lockfile.rename_local_deployed_path("old.md", "new.md")

    assert lockfile.deployment_ledger.records == {}
    assert lockfile._deployments_present is False


def test_outside_home_opencode_path_uses_target_relative_locator(tmp_path: Path) -> None:
    project_root = tmp_path / "apm-home"
    config_root = tmp_path / "opencode-config"
    target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir=config_root.as_posix(),
    )
    target_path = config_root / "agents" / "reviewer.md"

    entry = deployed_path_entry(target_path, project_root, [target])

    assert entry == "agents/reviewer.md"
    locator = DeploymentLedgerCodec.locator_for_path(
        target_path,
        project_root=project_root,
        target=target,
        scope="user",
    )
    assert locator.kind is LocatorKind.TARGET_RELATIVE
    assert locator.value == entry


def test_outside_home_opencode_user_locator_preserves_metadata_and_roundtrips(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "apm-home"
    config_root = tmp_path / "opencode-config"
    target = replace(
        KNOWN_TARGETS["opencode"].for_scope(user_scope=True),
        root_dir=config_root.as_posix(),
    )
    target_path = config_root / "skills" / "reviewer" / "SKILL.md"

    value = deployed_path_entry(
        target_path,
        project_root,
        [target],
        scope=InstallScope.USER,
    )
    locator = DeploymentLedgerCodec.locator_for_path(
        target_path,
        project_root=project_root,
        target=target,
        scope=InstallScope.USER,
    )

    assert value == "skills/reviewer/SKILL.md"
    assert locator.kind is LocatorKind.TARGET_RELATIVE
    assert locator.target == "opencode"
    assert locator.scope == "user"
    assert locator.value == value
    rows = DeploymentLedgerCodec.rows(
        DeploymentLedgerCodec.from_rows(
            [
                {
                    "kind": locator.kind.value,
                    "target": locator.target,
                    "value": locator.value,
                    "runtime": locator.runtime,
                    "scope": locator.scope,
                    "owners": ["pkg"],
                    "active_owner": "pkg",
                    "content_hash": None,
                }
            ]
        )
    )
    assert rows[0]["kind"] == "target-relative"
    assert rows[0]["target"] == "opencode"
    assert rows[0]["scope"] == "user"
