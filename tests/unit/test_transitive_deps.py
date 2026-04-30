"""Unit tests for transitive dependency handling.

Tests that:
- LockFile.installed_paths_for_project() correctly returns paths for all locked deps
- _check_orphaned_packages() does not flag transitive deps as orphaned
- get_dependency_declaration_order() includes transitive deps from lockfile
"""

from pathlib import Path

import yaml

from apm_cli.deps.lockfile import (
    LockedDependency,
    LockFile,
)
from apm_cli.primitives.discovery import get_dependency_declaration_order


class TestGetLockfileInstalledPaths:
    """Tests for get_lockfile_installed_paths helper."""

    def test_returns_empty_when_no_lockfile(self, tmp_path):
        """Returns empty list when no apm.lock exists."""
        assert LockFile.installed_paths_for_project(tmp_path) == []

    def test_returns_paths_for_regular_packages(self, tmp_path):
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo-a", depth=1))
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo-b", depth=2))
        lockfile.write(tmp_path / "apm.lock.yaml")

        paths = LockFile.installed_paths_for_project(tmp_path)
        assert "owner/repo-a" in paths
        assert "owner/repo-b" in paths

    def test_no_duplicates(self, tmp_path):
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo", depth=1))
        lockfile.write(tmp_path / "apm.lock.yaml")

        paths = LockFile.installed_paths_for_project(tmp_path)
        assert paths.count("owner/repo") == 1

    def test_ordered_by_depth_then_repo(self, tmp_path):
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="z/deep", depth=3))
        lockfile.add_dependency(LockedDependency(repo_url="a/direct", depth=1))
        lockfile.add_dependency(LockedDependency(repo_url="m/mid", depth=2))
        lockfile.write(tmp_path / "apm.lock.yaml")

        paths = LockFile.installed_paths_for_project(tmp_path)
        assert paths == ["a/direct", "m/mid", "z/deep"]

    def test_virtual_file_package_path(self, tmp_path):
        """Virtual file packages should use the flattened virtual package name."""
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                is_virtual=True,
                virtual_path="prompts/code-review.prompt.md",
                depth=1,
            )
        )
        lockfile.write(tmp_path / "apm.lock.yaml")

        paths = LockFile.installed_paths_for_project(tmp_path)
        # Virtual file: owner/<repo>-<stem> → owner/repo-code-review
        assert "owner/repo-code-review" in paths

    def test_corrupt_lockfile(self, tmp_path):
        """Corrupt lockfile should return empty list."""
        (tmp_path / "apm.lock.yaml").write_text("not: valid: yaml: [")
        assert LockFile.installed_paths_for_project(tmp_path) == []


class TestTransitiveDependencyDiscovery:
    """Test that transitive deps from lockfile appear in discovery order."""

    def _write_apm_yml(self, path: Path, deps: list):
        content = {
            "name": "test-project",
            "version": "1.0.0",
            "description": "test",
            "dependencies": {"apm": deps},
        }
        (path / "apm.yml").write_text(yaml.dump(content))

    def test_transitive_deps_appended_after_direct(self, tmp_path):
        self._write_apm_yml(tmp_path, ["owner/direct"])

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/direct", depth=1))
        lockfile.add_dependency(
            LockedDependency(
                repo_url="owner/transitive",
                depth=2,
                resolved_by="owner/direct",
            )
        )
        lockfile.write(tmp_path / "apm.lock.yaml")

        order = get_dependency_declaration_order(str(tmp_path))
        assert order == ["owner/direct", "owner/transitive"]

    def test_direct_deps_not_duplicated(self, tmp_path):
        self._write_apm_yml(tmp_path, ["owner/a", "owner/b"])

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/a", depth=1))
        lockfile.add_dependency(LockedDependency(repo_url="owner/b", depth=1))
        lockfile.write(tmp_path / "apm.lock.yaml")

        order = get_dependency_declaration_order(str(tmp_path))
        assert order == ["owner/a", "owner/b"]

    def test_multiple_transitive_levels(self, tmp_path):
        """Mirrors the exact scenario from the bug report."""
        self._write_apm_yml(tmp_path, ["rieraj/team-cot-agent-instructions"])

        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/team-cot-agent-instructions",
                depth=1,
            )
        )
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/division-ime-agent-instructions",
                depth=2,
                resolved_by="rieraj/team-cot-agent-instructions",
            )
        )
        lockfile.add_dependency(
            LockedDependency(
                repo_url="rieraj/autodesk-agent-instructions",
                depth=3,
                resolved_by="rieraj/division-ime-agent-instructions",
            )
        )
        lockfile.write(tmp_path / "apm.lock.yaml")

        order = get_dependency_declaration_order(str(tmp_path))
        assert len(order) == 3
        assert order[0] == "rieraj/team-cot-agent-instructions"
        assert "rieraj/division-ime-agent-instructions" in order
        assert "rieraj/autodesk-agent-instructions" in order

    def test_no_lockfile_falls_back_to_direct_only(self, tmp_path):
        self._write_apm_yml(tmp_path, ["owner/only-direct"])
        # No lockfile created

        order = get_dependency_declaration_order(str(tmp_path))
        assert order == ["owner/only-direct"]


class TestOrphanDetectionWithTransitiveDeps:
    """Test _check_orphaned_packages accounts for transitive deps."""

    def _setup_project(self, tmp_path, direct_deps, lockfile_deps, installed_pkgs):
        """Set up a project directory with apm.yml, apm.lock, and apm_modules."""
        # apm.yml
        content = {
            "name": "test-project",
            "version": "1.0.0",
            "description": "test",
            "dependencies": {"apm": direct_deps},
        }
        (tmp_path / "apm.yml").write_text(yaml.dump(content))

        # apm.lock
        if lockfile_deps:
            lockfile = LockFile()
            for dep in lockfile_deps:
                lockfile.add_dependency(dep)
            lockfile.write(tmp_path / "apm.lock.yaml")

        # apm_modules directories
        for pkg in installed_pkgs:
            pkg_dir = tmp_path / "apm_modules" / pkg
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "apm.yml").write_text(f"name: {pkg.split('/')[-1]}\nversion: 1.0.0\n")

    def test_transitive_dep_not_flagged_as_orphan(self, tmp_path, monkeypatch):
        """Transitive deps in apm.lock should NOT be flagged as orphaned."""
        self._setup_project(
            tmp_path,
            direct_deps=["rieraj/team-cot"],
            lockfile_deps=[
                LockedDependency(repo_url="rieraj/team-cot", depth=1),
                LockedDependency(
                    repo_url="rieraj/division-ime", depth=2, resolved_by="rieraj/team-cot"
                ),
                LockedDependency(
                    repo_url="rieraj/autodesk", depth=3, resolved_by="rieraj/division-ime"
                ),
            ],
            installed_pkgs=["rieraj/team-cot", "rieraj/division-ime", "rieraj/autodesk"],
        )

        monkeypatch.chdir(tmp_path)

        from apm_cli.commands._helpers import _check_orphaned_packages

        orphans = _check_orphaned_packages()
        assert orphans == [], f"Transitive deps should not be orphaned, got: {orphans}"

    def test_truly_orphaned_package_still_detected(self, tmp_path, monkeypatch):
        """Packages not in apm.yml or apm.lock should still be flagged."""
        self._setup_project(
            tmp_path,
            direct_deps=["owner/kept"],
            lockfile_deps=[
                LockedDependency(repo_url="owner/kept", depth=1),
            ],
            installed_pkgs=["owner/kept", "owner/stale"],
        )

        monkeypatch.chdir(tmp_path)

        from apm_cli.commands._helpers import _check_orphaned_packages

        orphans = _check_orphaned_packages()
        assert "owner/stale" in orphans

    def test_no_lockfile_still_works(self, tmp_path, monkeypatch):
        """Without a lockfile, orphan detection should still work (direct deps only)."""
        self._setup_project(
            tmp_path,
            direct_deps=["owner/kept"],
            lockfile_deps=None,
            installed_pkgs=["owner/kept", "owner/stale"],
        )

        monkeypatch.chdir(tmp_path)

        from apm_cli.commands._helpers import _check_orphaned_packages

        orphans = _check_orphaned_packages()
        assert "owner/stale" in orphans
