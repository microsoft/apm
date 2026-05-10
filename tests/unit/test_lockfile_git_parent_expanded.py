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


class TestParentExpansionToLockfilePipeline:
    """End-to-end glue: ``{ git: parent, path: ... }`` -> resolver -> lockfile.

    The individual halves are unit-tested separately (``test_git_parent_resolver``
    covers expansion; the ``TestLockfileExpandedGitParent`` block above covers
    persistence shape). This pins the **seam** between them so a refactor that
    drifts either side cannot silently break portability of monorepo siblings.

    The contract: feeding a real ``DependencyReference.parse_from_dict({"git":
    "parent", "path": ...})`` through ``APMDependencyResolver.expand_parent_repo_decl``
    and into ``LockedDependency.from_dependency_ref`` MUST yield the same
    persisted bytes (and same unique key) as if the user had hand-written the
    explicit ``git`` + ``path`` form -- with no ``parent`` sentinel anywhere.
    """

    def _parent(self, git_url: str, ref: str = "main"):
        from apm_cli.models.apm_package import DependencyReference as DR

        return DR.parse_from_dict({"git": git_url, "path": "agents/pkg-a", "ref": ref})

    def _child_parent_decl(self):
        from apm_cli.models.apm_package import DependencyReference as DR

        return DR.parse_from_dict({"git": "parent", "path": "skills/shared"})

    def _explicit(self, git_url: str, ref: str = "main"):
        from apm_cli.models.apm_package import DependencyReference as DR

        return DR.parse_from_dict({"git": git_url, "path": "skills/shared", "ref": ref})

    def _round_trip(self, dep_ref, *, sha: str = "f" * 40):
        lf = LockFile()
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit=sha,
            depth=2,
            resolved_by="org/parent-pkg",
        )
        lf.add_dependency(locked)
        parsed = LockFile.from_yaml(lf.to_yaml())
        reloaded = next(iter(parsed.dependencies.values()))
        return locked, reloaded

    def test_pipeline_github_default_host(self):
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        parent = self._parent("https://github.com/org/monorepo.git")
        child = self._child_parent_decl()
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)

        # Sanity: the expansion drops the parent sentinel before lockfile sees it.
        assert expanded.is_parent_repo_inheritance is False
        assert expanded.repo_url == "org/monorepo"
        assert expanded.host == "github.com"

        explicit = self._explicit("https://github.com/org/monorepo.git")
        locked_p, _ = self._round_trip(expanded)
        locked_e, _ = self._round_trip(explicit)
        assert locked_p.to_dict() == locked_e.to_dict()
        assert locked_p.get_unique_key() == locked_e.get_unique_key()

        d = locked_p.to_dict()
        # No parent sentinel persisted anywhere as durable identity.
        assert d.get("repo_url") != "parent" and d.get("repo_url") != "_parent"
        assert d.get("source") != "parent" and d.get("source") != "_parent"
        assert d.get("virtual_path") == "skills/shared"
        assert d.get("is_virtual") is True

    def test_pipeline_gitlab_class_host(self):
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        parent = self._parent("https://git.example.com/org/monorepo.git")
        child = self._child_parent_decl()
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)

        explicit = self._explicit("https://git.example.com/org/monorepo.git")
        locked_p, reloaded_p = self._round_trip(expanded, sha="a" * 40)
        locked_e, _ = self._round_trip(explicit, sha="a" * 40)
        assert locked_p.to_dict() == locked_e.to_dict()
        assert locked_p.get_unique_key() == locked_e.get_unique_key()

        # Survives YAML round-trip with expanded coordinates intact.
        assert reloaded_p.host == "git.example.com"
        assert reloaded_p.repo_url == "org/monorepo"
        assert reloaded_p.virtual_path == "skills/shared"
        assert reloaded_p.is_virtual is True

    def test_pipeline_ref_override_on_child(self):
        """Child ``ref:`` override survives expansion and reaches the lockfile."""
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import DependencyReference as DR

        parent = self._parent("https://github.com/org/monorepo.git", ref="main")
        child = DR.parse_from_dict({"git": "parent", "path": "skills/shared", "ref": "v1.2.3"})
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.reference == "v1.2.3"

        locked, reloaded = self._round_trip(expanded, sha="b" * 40)
        assert locked.resolved_ref == "v1.2.3"
        assert reloaded.resolved_ref == "v1.2.3"
