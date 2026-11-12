"""Legacy ``resolved_by`` parents name the locked parent entry (#3649)."""

from __future__ import annotations

from apm_cli.deps.lockfile import LockedDependency, lockfile_parent_identity
from apm_cli.models.dependency.reference import DependencyReference


def test_legacy_parent_identity_ignores_declared_casing() -> None:
    """A parent declared as ``Owner/Repo`` must match its ``owner/repo`` lock row,
    or deps tree / why lose the transitive edge."""
    declared = DependencyReference.parse("Test/C")
    locked = LockedDependency(repo_url="Test/C", host="github.com")

    assert lockfile_parent_identity(declared, None) == "test/c"
    assert lockfile_parent_identity(locked, None) == "test/c"


def test_replacement_lock_parent_identity_stays_the_unique_key() -> None:
    declared = DependencyReference.parse("Test/C/skills/x")

    assert lockfile_parent_identity(declared, []) == declared.get_unique_key()


def test_case_sensitive_host_parent_identity_keeps_casing() -> None:
    declared = DependencyReference.parse("gitlab.example.com/Team/Repo")

    assert lockfile_parent_identity(declared, None) == "Team/Repo"
