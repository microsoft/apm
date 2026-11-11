"""Tests for revision-pin lockfile metadata carry-forward."""

from types import SimpleNamespace

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


def test_carry_forward_resolved_tag_for_unchanged_sha() -> None:
    """Plain install preserves the tag label for unchanged SHA-pinned deps."""
    sha = "a" * 40
    existing = LockFile()
    existing.add_dependency(
        LockedDependency(
            repo_url="org/pkg",
            resolved_ref=sha,
            resolved_commit=sha,
            resolved_tag="v1.2.0",
        )
    )
    new = LockFile()
    new.add_dependency(
        LockedDependency(
            repo_url="org/pkg",
            resolved_ref=sha,
            resolved_commit=sha,
        )
    )
    ctx = SimpleNamespace(existing_lockfile=existing)

    LockfileBuilder(ctx)._preserve_existing_revision_pin_tags(new)

    assert new.get_dependency("org/pkg").resolved_tag == "v1.2.0"


def test_carried_tag_resyncs_version_for_semantic_comparison() -> None:
    """A carried resolved_tag must not leave an unchanged v2 lock looking changed."""
    sha = "a" * 40
    existing = LockFile()
    existing.add_dependency(
        LockedDependency(
            repo_url="org/pkg",
            resolved_ref=sha,
            resolved_commit=sha,
            resolved_tag="v1.2.0",
        )
    )
    committed = LockFile.from_yaml(existing.to_yaml())
    assert committed.lockfile_version == "2"
    new = LockFile()
    new.add_dependency(LockedDependency(repo_url="org/pkg", resolved_ref=sha, resolved_commit=sha))
    LockfileBuilder(
        SimpleNamespace(existing_lockfile=committed)
    )._preserve_existing_revision_pin_tags(new)
    assert new.lockfile_version == "1"

    new.sync_lockfile_version()

    assert new.lockfile_version == "2"
    assert new.is_semantically_equivalent(committed)


def test_sync_lockfile_version_keeps_v1_without_v2_fields() -> None:
    """Re-deriving the version never promotes a lock that has no v2 content."""
    sha = "b" * 40
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="org/pkg", resolved_ref=sha, resolved_commit=sha))

    lock.sync_lockfile_version()

    assert lock.lockfile_version == "1"
