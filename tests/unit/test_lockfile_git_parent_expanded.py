"""Lockfile persistence for expanded ``git: parent`` dependencies.

Resolver expands ``{ git: parent, path: ... }`` to the same coordinates as an
explicit virtual git dependency. The lockfile must persist those expanded fields
and never use ``parent`` as durable ``repo_url`` / ``source`` identity.
"""

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import DependencyReference


def _expanded_parent_equivalent_ref() -> DependencyReference:
    """Simulate post-resolver ref: same shape as explicit ``git`` + ``path``."""
    return DependencyReference(
        repo_url="org/monorepo",
        host="git.example.com",
        reference="main",
        virtual_path="skills/shared",
        is_virtual=True,
        is_parent_repo_inheritance=False,
    )


def _explicit_equivalent_ref() -> DependencyReference:
    """Explicit virtual subdirectory dep with identical coordinates."""
    return DependencyReference(
        repo_url="org/monorepo",
        host="git.example.com",
        reference="main",
        virtual_path="skills/shared",
        is_virtual=True,
    )


class TestLockfileExpandedGitParent:
    def test_from_dependency_ref_copies_expanded_coordinates(self):
        dep_ref = _expanded_parent_equivalent_ref()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit="a" * 40,
            depth=2,
            resolved_by="org/monorepo/agents/pkg-a",
            is_dev=False,
        )
        assert locked.repo_url == "org/monorepo"
        assert locked.host == "git.example.com"
        assert locked.virtual_path == "skills/shared"
        assert locked.is_virtual is True
        assert locked.resolved_ref == "main"
        assert locked.resolved_commit == "a" * 40
        assert locked.depth == 2
        assert locked.resolved_by == "org/monorepo/agents/pkg-a"
        assert locked.source is None

    def test_to_dict_has_no_parent_sentinel(self):
        locked = LockedDependency.from_dependency_ref(
            dep_ref=_expanded_parent_equivalent_ref(),
            resolved_commit="b" * 40,
            depth=2,
            resolved_by="org/parent-pkg",
        )
        d = locked.to_dict()
        assert d["repo_url"] == "org/monorepo"
        assert d["repo_url"] != "parent"
        assert d.get("source") != "parent"
        assert d["host"] == "git.example.com"
        assert d["virtual_path"] == "skills/shared"
        assert d["is_virtual"] is True
        assert d["resolved_ref"] == "main"
        assert d["resolved_commit"] == "b" * 40

    def test_round_trip_dict_preserves_fields_and_unique_key(self):
        original = LockedDependency.from_dependency_ref(
            dep_ref=_expanded_parent_equivalent_ref(),
            resolved_commit="c" * 40,
            depth=2,
            resolved_by="org/parent-pkg",
        )
        key_before = original.get_unique_key()
        restored = LockedDependency.from_dict(original.to_dict())
        assert restored.get_unique_key() == key_before
        assert restored.repo_url == original.repo_url
        assert restored.host == original.host
        assert restored.virtual_path == original.virtual_path
        assert restored.is_virtual == original.is_virtual
        assert restored.resolved_ref == original.resolved_ref
        assert restored.resolved_commit == original.resolved_commit
        assert restored.depth == original.depth
        assert restored.resolved_by == original.resolved_by

    def test_lockfile_yaml_round_trip_preserves_unique_key(self):
        lf = LockFile()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=_expanded_parent_equivalent_ref(),
            resolved_commit="d" * 40,
            depth=2,
            resolved_by="org/parent-pkg",
        )
        key_before = locked.get_unique_key()
        lf.add_dependency(locked)
        assert locked.get_unique_key() == key_before

        parsed = LockFile.from_yaml(lf.to_yaml())
        assert len(parsed.dependencies) == 1
        reloaded = next(iter(parsed.dependencies.values()))
        assert reloaded.get_unique_key() == key_before
        assert reloaded.repo_url == "org/monorepo"
        assert reloaded.host == "git.example.com"
        assert reloaded.virtual_path == "skills/shared"
        assert reloaded.is_virtual is True

    def test_expanded_parent_matches_explicit_virtual_lock_shape(self):
        """Same persisted shape as an explicit ``git`` + ``path`` virtual dep."""
        a = LockedDependency.from_dependency_ref(
            _expanded_parent_equivalent_ref(),
            resolved_commit="e" * 40,
            depth=2,
            resolved_by="x",
        )
        b = LockedDependency.from_dependency_ref(
            _explicit_equivalent_ref(),
            resolved_commit="e" * 40,
            depth=2,
            resolved_by="x",
        )
        assert a.to_dict() == b.to_dict()
        assert a.get_unique_key() == b.get_unique_key()
