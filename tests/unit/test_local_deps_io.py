"""Unit tests for local filesystem path dependency support (filesystem, process and CLI boundary cases)."""

from pathlib import Path  # noqa: F401
from unittest.mock import Mock  # noqa: F401

import pytest
import yaml

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, DependencyReference

pytestmark = pytest.mark.component

# ===========================================================================
# DependencyReference.is_local_path()
# ===========================================================================


# ===========================================================================
# DependencyReference.parse() with local paths
# ===========================================================================


# ===========================================================================
# DependencyReference methods for local deps
# ===========================================================================


class TestLocalDepMethods:
    """Test DependencyReference methods with local dependencies."""

    def test_get_install_path(self, tmp_path):
        dep = DependencyReference.parse("./packages/my-skills")
        install_path = dep.get_install_path(tmp_path / "apm_modules")
        assert install_path == tmp_path / "apm_modules" / "_local" / "my-skills"

    def test_install_path_no_conflict_with_remote(self, tmp_path):
        """Local and remote packages with same name should not conflict."""
        local_dep = DependencyReference.parse("./skills")
        remote_dep = DependencyReference.parse("owner/skills")
        apm_modules = tmp_path / "apm_modules"
        assert local_dep.get_install_path(apm_modules) != remote_dep.get_install_path(apm_modules)


# ===========================================================================
# LockedDependency with local source
# ===========================================================================


class TestLockedDependencyLocal:
    """Test LockedDependency serialization for local path dependencies."""

    def test_round_trip(self, tmp_path):
        """Write and read back a lockfile with local dependencies."""
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/my-skills",
                source="local",
                local_path="./packages/my-skills",
                deployed_files=[".github/instructions/my-skill.instructions.md"],
            )
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="owner/remote-pkg",
                resolved_commit="abc123",
                deployed_files=[".github/instructions/remote.instructions.md"],
            )
        )

        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)
        loaded = LockFile.read(lock_path)

        assert loaded.has_dependency("./packages/my-skills")
        assert loaded.has_dependency("owner/remote-pkg")

        local_dep = loaded.get_dependency("./packages/my-skills")
        assert local_dep.source == "local"
        assert local_dep.local_path == "./packages/my-skills"

        remote_dep = loaded.get_dependency("owner/remote-pkg")
        assert remote_dep.source is None
        assert remote_dep.resolved_commit == "abc123"


# ===========================================================================
# APMPackage.from_apm_yml with local deps
# ===========================================================================


class TestAPMPackageLocalDeps:
    """Test APMPackage loading with local path dependencies in apm.yml."""

    def test_apm_yml_with_local_string_dep(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["./packages/my-skills"],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].is_local is True
        assert deps[0].local_path == "./packages/my-skills"

    def test_apm_yml_with_local_dict_dep(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"path": "./packages/my-skills"}],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].is_local is True
        assert deps[0].local_path == "./packages/my-skills"

    def test_mixed_local_and_remote_deps(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            "microsoft/apm-sample-package",
                            "./packages/my-local-skills",
                            "/absolute/path/to/pkg",
                        ],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 3
        assert deps[0].is_local is False
        assert deps[1].is_local is True
        assert deps[1].local_path == "./packages/my-local-skills"
        assert deps[2].is_local is True
        assert deps[2].local_path == "/absolute/path/to/pkg"

    def test_invalid_dict_path_rejected(self, tmp_path):
        """Dict-form paths that don't look like filesystem paths should be rejected."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"path": "not-a-local-path"}],
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="local filesystem path"):
            APMPackage.from_apm_yml(apm_yml)


# ===========================================================================
# Pack guard: reject local deps
# ===========================================================================


class TestPackGuardLocalDeps:
    """Test that packing rejects packages with local dependencies."""

    def test_pack_rejects_local_deps(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        # Set up project with local dep
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["./packages/my-local-pkg"],
                    },
                }
            )
        )

        # Create a lockfile so pack_bundle doesn't fail on missing lockfile
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/my-local-pkg",
                source="local",
                local_path="./packages/my-local-pkg",
            )
        )
        lock.write(tmp_path / "apm.lock")

        with pytest.raises(ValueError, match="local path dependency"):
            pack_bundle(tmp_path, tmp_path / "dist")

    def test_pack_allows_remote_deps(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        # Set up project with only remote dep
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["owner/repo"],
                    },
                }
            )
        )

        # Create an empty lockfile
        lock = LockFile()
        lock.write(tmp_path / "apm.lock")

        # Should not raise (may fail for other reasons like missing files, that's OK)
        # We just check it gets past the guard
        try:
            pack_bundle(tmp_path, tmp_path / "dist")
        except ValueError as e:
            assert "local path dependency" not in str(e)


# ===========================================================================
# Copy local package helper
# ===========================================================================


class TestCopyLocalPackage:
    """Test the _copy_local_package helper from the install module."""

    def test_copy_local_package_with_apm_yml(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a local package
        local_pkg = tmp_path / "my-local-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-local-pkg",
                    "version": "1.0.0",
                }
            )
        )
        instr_dir = local_pkg / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "test.instructions.md").write_text("# Test")

        # Create dep ref and install path
        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)  # Use absolute path for test
        install_path = tmp_path / "apm_modules" / "_local" / "my-local-pkg"

        result = _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert result is not None
        assert result.exists()
        assert (result / "apm.yml").exists()
        assert (result / ".apm" / "instructions" / "test.instructions.md").exists()

    def test_copy_local_package_logger_none_does_not_raise(self, tmp_path):
        """PR #1111 review C1: _copy_local_package must tolerate logger=None.

        ``run_install_pipeline(logger=None)`` is a public, documented entry
        point; the helper must not AttributeError when reached without an
        explicit InstallLogger threaded through.
        """
        from apm_cli.commands.install import _copy_local_package

        # Trigger the failure branch (missing path) -- this branch calls
        # ``logger.error(...)``. Before the fix it would AttributeError on
        # ``None.error``; now it gracefully falls back to NullCommandLogger.
        dep_ref = DependencyReference.parse("./does-not-exist")
        install_path = tmp_path / "apm_modules" / "_local" / "does-not-exist"

        result = _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=None
        )
        assert result is None  # missing path fails as expected, no AttributeError

    def test_copy_local_package_with_skill_md(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a Claude Skill package (SKILL.md but no apm.yml)
        local_pkg = tmp_path / "my-skill"
        local_pkg.mkdir()
        (local_pkg / "SKILL.md").write_text("# My Skill")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "my-skill"

        result = _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert result is not None
        assert (result / "SKILL.md").exists()

    def test_copy_local_package_missing_path(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        dep_ref = DependencyReference.parse("./nonexistent-pkg")
        install_path = tmp_path / "apm_modules" / "_local" / "nonexistent-pkg"

        result = _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert result is None

    def test_copy_local_package_no_manifest(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a directory without apm.yml or SKILL.md
        local_pkg = tmp_path / "no-manifest"
        local_pkg.mkdir()
        (local_pkg / "README.md").write_text("# No manifest")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "no-manifest"

        result = _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert result is None

    def test_copy_replaces_existing(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a local package
        local_pkg = tmp_path / "my-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-pkg",
                    "version": "1.0.0",
                }
            )
        )
        (local_pkg / "data.txt").write_text("original")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "my-pkg"

        # First copy
        _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert (install_path / "data.txt").read_text() == "original"

        # Modify source
        (local_pkg / "data.txt").write_text("updated")

        # Second copy should overwrite
        _copy_local_package(
            dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
        )
        assert (install_path / "data.txt").read_text() == "updated"

    def test_copy_preserves_symlinks_without_following(self, tmp_path):
        """Symlinks escaping the package root must hard-fail (not be silently preserved).

        Before #1668: shutil.copytree with symlinks=True preserved the link in
        apm_modules/ -- an escaping link was inert there but could still confuse
        consumers.  After #1668: any symlink whose resolved target escapes the
        package root raises PathTraversalError immediately.  This test was
        previously asserting the old (preserved-as-symlink) behavior; it is
        updated to assert the correct hard-fail behavior.
        """
        from apm_cli.commands.install import _copy_local_package
        from apm_cli.utils.path_security import PathTraversalError

        # Create a secret file outside the package
        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        (secret_dir / "credentials.txt").write_text("TOP_SECRET")

        # Create a local package with a symlink pointing outside
        local_pkg = tmp_path / "evil-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "evil-pkg",
                    "version": "1.0.0",
                }
            )
        )
        (local_pkg / "escape").symlink_to(secret_dir)

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "evil-pkg"

        with pytest.raises(PathTraversalError, match=r"(?i)escape|outside|traversal"):
            _copy_local_package(
                dep_ref, install_path, tmp_path, project_root=tmp_path, logger=NullCommandLogger()
            )


# ===========================================================================
# #857 Regression: transitive local_path anchor + dual-reject from remote
# ===========================================================================


class TestSourcePathField:
    """APMPackage.source_path is the anchor for relative deps (#857)."""

    def test_from_apm_yml_threads_source_path(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "x", "version": "1.0.0"}))
        anchor = (tmp_path / "anchor").resolve()
        anchor.mkdir()

        pkg = APMPackage.from_apm_yml(apm_yml, source_path=anchor)
        assert pkg.source_path == anchor

    def test_cache_key_distinguishes_source_path(self, tmp_path):
        """Same apm.yml loaded with different source_path values must NOT
        collide in the module-level cache (#940 F1)."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "x", "version": "1.0.0"}))
        a = (tmp_path / "a").resolve()
        a.mkdir()
        b = (tmp_path / "b").resolve()
        b.mkdir()

        pkg_a = APMPackage.from_apm_yml(apm_yml, source_path=a)
        pkg_b = APMPackage.from_apm_yml(apm_yml, source_path=b)
        # Different cache entries -> different source_path on each instance.
        assert pkg_a.source_path == a
        assert pkg_b.source_path == b


class TestCopyLocalPackageContainmentBoundary:
    """_copy_local_package no longer enforces project-root containment
    on local-from-local sources (#940 follow-up). The untrusted-source
    boundary lives upstream in apm_resolver._try_load_dependency_package
    via dual-reject of remote-parent local_paths. Sibling layouts like
    ``apm install ../pkg-a`` from a monorepo workspace are explicit user
    opt-ins and must succeed.
    """

    def test_allows_sibling_outside_project_root(self, tmp_path):
        """Sibling layout (declared by user, anchored on project_root)
        must succeed even when the resolved path lies outside
        project_root.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        outside = tmp_path / "outside-pkg"
        outside.mkdir()
        (outside / "apm.yml").write_text(yaml.safe_dump({"name": "outside", "version": "1.0.0"}))

        project_root = tmp_path / "project"
        project_root.mkdir()

        dep_ref = DependencyReference.parse("../outside-pkg")
        install_path = project_root / "apm_modules" / "_local" / "outside-pkg"

        result = _copy_local_package(
            dep_ref,
            install_path,
            project_root,
            project_root=project_root,
            logger=NullCommandLogger(),
        )
        assert result == install_path
        assert install_path.exists()
        assert (install_path / "apm.yml").exists()


class TestDepBaseDirsCrossPhase:
    """The resolve phase must populate ctx.dep_base_dirs with parent
    source_path anchors so the integrate phase can copy transitive locals
    from the right base dir (#857). This pins the cross-phase contract.
    """

    def test_dep_base_dirs_anchors_transitive_on_parent_source_path(self, tmp_path):
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        # Build a tiny synthetic dependency tree manually so the contract
        # check doesn't depend on the resolve phase's many other moving
        # parts (lockfile, downloader, etc). The map-build logic lives in
        # resolve.py but its only input is dependency_graph.dependency_tree.
        from apm_cli.deps.dependency_graph import (
            DependencyNode,
            DependencyTree,
        )
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        # Mimic the resolve-phase walker
        root_pkg = APMPackage(name="root", version="1.0.0")
        root_pkg.source_path = tmp_path
        mid_pkg = APMPackage(name="mid", version="1.0.0")
        mid_pkg.source_path = tmp_path / "packages" / "mid"

        root_dep = DependencyReference.parse("./packages/mid")
        leaf_dep = DependencyReference.parse("../base")

        tree = DependencyTree(root_package=root_pkg)
        root_node = DependencyNode(package=root_pkg, dependency_ref=root_dep, depth=0)
        mid_node = DependencyNode(
            package=mid_pkg, dependency_ref=root_dep, depth=1, parent=root_node
        )
        leaf_node = DependencyNode(
            package=APMPackage(name="base", version="1.0.0"),
            dependency_ref=leaf_dep,
            depth=2,
            parent=mid_node,
        )
        tree.add_node(root_node)
        tree.add_node(mid_node)
        tree.add_node(leaf_node)

        # Inline the map build (mirrors resolve.py exactly)
        dep_base_dirs: dict = {}
        for node in tree.nodes.values():
            parent_node = node.parent
            if parent_node is None or parent_node.package is None:
                continue
            anchor = (
                parent_node.package.source_path
                if parent_node.package.source_path is not None
                else tmp_path
            )
            dep_base_dirs[node.dependency_ref.get_unique_key()] = anchor

        # Leaf dep must anchor on MID's source_path, NOT root's.
        assert dep_base_dirs[leaf_dep.get_unique_key()] == mid_pkg.source_path
        # mid_pkg.source_path != tmp_path (the project root)
        assert dep_base_dirs[leaf_dep.get_unique_key()] != tmp_path

        # Sanity: APMDependencyResolver instantiable without error
        APMDependencyResolver()
