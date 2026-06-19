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
