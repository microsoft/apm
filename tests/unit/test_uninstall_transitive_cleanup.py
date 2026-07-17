"""Tests for transitive dependency cleanup during uninstall.

npm-style behavior: when uninstalling a package that brought in transitive
dependencies, those transitive deps should also be removed if no other
remaining package still needs them.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest  # noqa: F401
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency import DependencyReference


def _write_apm_yml(path: Path, deps: list[str]):
    """Write a minimal apm.yml with given APM dependencies."""
    data = {
        "name": "test-project",
        "version": "1.0.0",
        "dependencies": {"apm": deps},
    }
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def _write_lockfile(path: Path, locked_deps: list[LockedDependency]):
    """Write a lockfile with given locked dependencies."""
    lockfile = LockFile()
    for dep in locked_deps:
        lockfile.add_dependency(dep)
    lockfile.write(path)


def _make_apm_modules_dir(base: Path, repo_url: str, deps: list[str] | None = None):
    """Create a minimal package directory under apm_modules/.

    ``deps``, when given, is written as this package's own real
    ``dependencies.apm`` list -- required for the forward-reachability
    walk (see ``apm_cli.deps.reachability``) to find a genuine surviving
    transitive edge; without it, this package's on-disk manifest declares
    no dependencies at all, matching the pre-fix behavior every existing
    test in this file (before the diamond-specific additions below) relies
    on.
    """
    parts = repo_url.split("/")
    pkg_dir = base / "apm_modules"
    for part in parts:
        pkg_dir = pkg_dir / part
    pkg_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {parts[-1]}", "version: 1.0.0"]
    if deps:
        lines.append("dependencies:")
        lines.append("  apm:")
        for dep in deps:
            lines.append(f"    - {dep}")
    (pkg_dir / "apm.yml").write_text("\n".join(lines) + "\n")
    return pkg_dir


class TestUninstallTransitiveDependencyCleanup:
    """Uninstalling a package removes its orphaned transitive dependencies."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).parent.parent.parent))

    def test_uninstall_removes_transitive_dep(self):
        """Uninstalling pkg-a also removes pkg-a's transitive dep pkg-b."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                # Setup: pkg-a depends on (transitive) pkg-b
                _write_apm_yml(root / "apm.yml", ["acme/pkg-a"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-b")  # transitive dep

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(
                            repo_url="acme/pkg-b",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="bbb",
                        ),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                # Both direct and transitive should be removed
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
                assert not (root / "apm_modules" / "acme" / "pkg-b").exists()
                assert "transitive dependency" in result.output.lower()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_keeps_shared_transitive_dep(self):
        """Transitive dep used by another remaining package is NOT removed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                # Setup: both pkg-a and pkg-c depend on (transitive) shared-lib
                _write_apm_yml(root / "apm.yml", ["acme/pkg-a", "acme/pkg-c"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-c")
                _make_apm_modules_dir(root, "acme/shared-lib")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"),
                        LockedDependency(
                            repo_url="acme/shared-lib",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="sss",
                        ),
                    ],
                )

                # Uninstall only pkg-a
                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
                # This fixture's pkg-c (created via _make_apm_modules_dir, which
                # writes a bare "name/version"-only apm.yml with no dependencies
                # section) does NOT actually declare shared-lib in its own
                # on-disk manifest, so shared-lib is genuinely unreachable from
                # any surviving package once pkg-a is gone -- removal here is
                # correct, both before and after the forward-reachability fix
                # (see apm_cli.deps.reachability). This is NOT "shared deps are
                # always removed regardless of other declared parents" as a
                # general rule: see test_uninstall_keeps_shared_remote_
                # transitive_dep_when_other_parent_survives below, where pkg-c's
                # real manifest DOES declare shared-lib and it correctly
                # survives instead.
                assert not (root / "apm_modules" / "acme" / "shared-lib").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_removes_deeply_nested_transitive_deps(self):
        """Transitive deps of transitive deps are also removed (recursive)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                # Setup: pkg-a -> pkg-b -> pkg-c (chain of transitive deps)
                _write_apm_yml(root / "apm.yml", ["acme/pkg-a"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-b")
                _make_apm_modules_dir(root, "acme/pkg-c")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(
                            repo_url="acme/pkg-b",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="bbb",
                        ),
                        LockedDependency(
                            repo_url="acme/pkg-c",
                            depth=3,
                            resolved_by="acme/pkg-b",
                            resolved_commit="ccc",
                        ),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
                assert not (root / "apm_modules" / "acme" / "pkg-b").exists()
                assert not (root / "apm_modules" / "acme" / "pkg-c").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_updates_lockfile(self):
        """Lockfile is updated to remove uninstalled deps and their transitives."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a", "acme/pkg-d"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-b")
                _make_apm_modules_dir(root, "acme/pkg-d")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(
                            repo_url="acme/pkg-b",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="bbb",
                        ),
                        LockedDependency(repo_url="acme/pkg-d", depth=1, resolved_commit="ddd"),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                # Lockfile should still exist with pkg-d
                updated_lock = LockFile.read(root / "apm.lock.yaml")
                assert updated_lock is not None
                assert updated_lock.has_dependency("acme/pkg-d")
                assert not updated_lock.has_dependency("acme/pkg-a")
                assert not updated_lock.has_dependency("acme/pkg-b")
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_removes_lockfile_when_no_deps_remain(self):
        """Lockfile is deleted when all deps are removed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a"])
                _make_apm_modules_dir(root, "acme/pkg-a")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                assert not (root / "apm.lock.yaml").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_dry_run_shows_transitive_deps(self):
        """Dry run shows transitive deps that would be removed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-b")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(
                            repo_url="acme/pkg-b",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="bbb",
                        ),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a", "--dry-run"])

                assert result.exit_code == 0
                assert "acme/pkg-b" in result.output
                assert "transitive" in result.output.lower()
                # Verify nothing was actually removed
                assert (root / "apm_modules" / "acme" / "pkg-a").exists()
                assert (root / "apm_modules" / "acme" / "pkg-b").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_no_lockfile_still_works(self):
        """Uninstall works gracefully when no lockfile exists (no transitive cleanup)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a"])
                _make_apm_modules_dir(root, "acme/pkg-a")

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_dry_run_supports_object_style_dependency_entries(self):
        """Dry-run accepts dict dependency entries without crashing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                data = {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"git": "acme/pkg-a"}],
                    },
                }
                (root / "apm.yml").write_text(
                    yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
                )
                _make_apm_modules_dir(root, "acme/pkg-a")

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a", "--dry-run"])

                assert result.exit_code == 0
                assert "Dry run complete" in result.output
                assert (root / "apm_modules" / "acme" / "pkg-a").exists()
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_reintegrates_remaining_object_style_dependency_from_canonical_path(self):
        """Remaining dict-style deps re-integrate from DependencyReference install paths."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                remaining_dep_entry = {
                    "git": "acme/pkg-b",
                    "path": "prompts/review.prompt.md",
                }
                data = {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            {"git": "acme/pkg-a"},
                            remaining_dep_entry,
                        ],
                    },
                }
                (root / "apm.yml").write_text(
                    yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
                )

                _make_apm_modules_dir(root, "acme/pkg-a")
                remaining_ref = DependencyReference.parse_from_dict(remaining_dep_entry)
                remaining_install_path = remaining_ref.get_install_path(Path("apm_modules"))
                (root / remaining_install_path).mkdir(parents=True, exist_ok=True)

                observed_paths = []

                def _capture_validate(path: Path):
                    observed_paths.append(path)
                    return SimpleNamespace(
                        package=APMPackage(name="pkg-b-review", version="1.0.0"),
                        package_type=None,
                    )

                with (
                    patch(
                        "apm_cli.models.apm_package.validate_apm_package",
                        side_effect=_capture_validate,
                    ),
                    patch(
                        "apm_cli.integration.targets.active_targets",
                        return_value=[],
                    ),
                    patch(
                        "apm_cli.integration.skill_integrator.SkillIntegrator.integrate_package_skill",
                        return_value=None,
                    ),
                ):
                    result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0
                assert remaining_install_path in observed_paths
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_keeps_shared_remote_transitive_dep_when_other_parent_survives(self):
        """Remote/registry diamond: a shared transitive dep survives if a
        surviving package's REAL on-disk manifest still declares it.

        Unlike test_uninstall_keeps_shared_transitive_dep above (whose
        pkg-c does not actually declare shared-lib), here pkg-c's own
        apm.yml genuinely lists acme/shared-lib as a dependency, so the
        forward-reachability walk (apm_cli.deps.reachability) must rescue
        it even though the lockfile's resolved_by still (first-wins)
        points at pkg-a, the package being removed.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a", "acme/pkg-c"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                # pkg-c's real manifest genuinely declares shared-lib.
                _make_apm_modules_dir(root, "acme/pkg-c", deps=["acme/shared-lib"])
                _make_apm_modules_dir(root, "acme/shared-lib")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"),
                        LockedDependency(
                            repo_url="acme/shared-lib",
                            depth=2,
                            resolved_by="acme/pkg-a",  # first-wins, no longer a survivor
                            resolved_commit="sss",
                        ),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0, result.output
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
                assert (root / "apm_modules" / "acme" / "shared-lib").exists()

                updated_lock = LockFile.read(root / "apm.lock.yaml")
                assert updated_lock is not None
                assert updated_lock.has_dependency("acme/shared-lib")
                # The rescue must repair resolved_by to the currently-valid
                # surviving parent -- otherwise the NEXT uninstall's backward
                # orphan-candidate scan (keyed on resolved_by) could never
                # find shared-lib again once pkg-a's own entry is gone.
                shared_dep = updated_lock.get_dependency("acme/shared-lib")
                assert shared_dep.resolved_by == "acme/pkg-c"
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_removes_shared_remote_transitive_dep_once_last_parent_removed(self):
        """Negative twin: once pkg-c (the last real parent) is ALSO removed
        in a later, separate invocation, shared-lib IS garbage collected.

        Must be one scenario with two sequential uninstalls against the
        SAME project state -- only that shape exercises the repaired
        resolved_by from the first uninstall (see the test above).
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a", "acme/pkg-c"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-c", deps=["acme/shared-lib"])
                _make_apm_modules_dir(root, "acme/shared-lib")

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"),
                        LockedDependency(
                            repo_url="acme/shared-lib",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="sss",
                        ),
                    ],
                )

                first = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])
                assert first.exit_code == 0, first.output
                assert (root / "apm_modules" / "acme" / "shared-lib").exists()

                second = self.runner.invoke(cli, ["uninstall", "acme/pkg-c"])
                assert second.exit_code == 0, second.output
                assert not (root / "apm_modules" / "acme" / "shared-lib").exists()

                lockfile_path = root / "apm.lock.yaml"
                if lockfile_path.exists():
                    remaining_lock = LockFile.read(lockfile_path)
                    assert not remaining_lock.has_dependency("acme/shared-lib")
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup

    def test_uninstall_preserves_candidates_when_remote_install_path_escapes(self):
        """Fail-closed: a remote child dep whose real install location is a
        symlink escaping apm_modules/ preserves candidate orphans rather
        than silently deleting or skipping them.

        Mirrors test_fails_closed_on_remote_install_path_symlink_escape in
        tests/unit/test_deps_reachability.py at the full CLI-integration
        level: pkg-c legitimately declares shared-lib, but shared-lib's
        directory under apm_modules/ is a symlink pointing outside
        apm_modules/, so get_install_path() raises PathTraversalError
        (a ValueError subclass) during the walk.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                root = Path(tmp_dir)

                _write_apm_yml(root / "apm.yml", ["acme/pkg-a", "acme/pkg-c"])
                _make_apm_modules_dir(root, "acme/pkg-a")
                _make_apm_modules_dir(root, "acme/pkg-c", deps=["acme/shared-lib"])

                outside = root / "outside_evil"
                outside.mkdir()
                (outside / "apm.yml").write_text("name: evil\nversion: 1.0.0\n")
                shared_lib_dir = root / "apm_modules" / "acme" / "shared-lib"
                shared_lib_dir.parent.mkdir(parents=True, exist_ok=True)
                shared_lib_dir.symlink_to(outside)
                clear_apm_yml_cache()

                _write_lockfile(
                    root / "apm.lock.yaml",
                    [
                        LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"),
                        LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"),
                        LockedDependency(
                            repo_url="acme/shared-lib",
                            depth=2,
                            resolved_by="acme/pkg-a",
                            resolved_commit="sss",
                        ),
                    ],
                )

                result = self.runner.invoke(cli, ["uninstall", "acme/pkg-a"])

                assert result.exit_code == 0, result.output
                assert not (root / "apm_modules" / "acme" / "pkg-a").exists()
                assert "could not be verified" in " ".join(result.output.split())

                updated_lock = LockFile.read(root / "apm.lock.yaml")
                assert updated_lock is not None
                assert updated_lock.has_dependency("acme/shared-lib"), (
                    "shared-lib must be preserved when its install path cannot be safely verified"
                )
            finally:
                os.chdir(
                    os.path.dirname(os.path.abspath(__file__))
                )  # restore CWD before TemporaryDirectory cleanup
