"""Regression-trap tests for issue #1668.

Verifies that local-path install dereferences in-package symlinks (parity
with remote install) and hard-fails on symlinks that escape the package root.

Coverage gates (typed: bug):
  - Regression trap: in-package symlink is materialized as a real file
  - Security trap: escaping symlink hard-fails the install
  - Baseline: non-symlink files are copied correctly (unchanged behavior)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.utils.path_security import PathTraversalError


def _try_symlink(link: Path, target: Path) -> None:
    """Create a symlink or skip the test on platforms without support."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this platform")


def _make_dep_ref(local_path: str):
    """Return a minimal DependencyReference-like object for testing."""

    class _FakeDep:
        def __init__(self, path: str) -> None:
            self.local_path = path
            self.is_local = True

    return _FakeDep(local_path)


def _make_valid_package(root: Path) -> None:
    """Populate *root* with a minimal valid APM package structure."""
    (root / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n", encoding="utf-8")
    skill_dir = root / ".apm" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "local.md").write_text("# Local reference\n", encoding="utf-8")


class TestInPackageSymlinkIsDerefed:
    """Regression trap: an in-package symlink must be materialized as a real
    file after _copy_local_package (not dropped, not left as a symlink)."""

    def test_in_package_symlink_materialized_as_real_file(self, tmp_path: Path) -> None:
        """_copy_local_package materializes an in-package symlink as a real file.

        Before the fix, shutil.copytree(..., symlinks=True) preserved the
        symlink, and the downstream ignore_non_content filter then silently
        dropped it from the deploy target.  After the fix the staged copy
        must contain a regular file, not a symlink.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        shared_dir = pkg_root / ".apm" / "shared"
        shared_dir.mkdir(parents=True)
        shared_file = shared_dir / "shared-contract.md"
        shared_file.write_text("# Shared contract\n", encoding="utf-8")

        # Symlink inside the package -> target resolves within pkg_root
        refs_dir = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        symlink = refs_dir / "shared-contract.md"
        # Relative symlink: ../../../shared/shared-contract.md
        _try_symlink(symlink, Path("../../../shared/shared-contract.md"))

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        result = _copy_local_package(
            dep_ref,
            install_path,
            tmp_path,
            project_root=tmp_path,
            logger=NullCommandLogger(),
        )

        assert result is not None, "_copy_local_package should succeed for in-package symlink"

        staged_symlink = (
            result / ".apm" / "skills" / "demo-skill" / "references" / "shared-contract.md"
        )
        assert staged_symlink.exists(), "Dereferenced file must exist in the staged copy"
        assert not staged_symlink.is_symlink(), (
            "Staged path must be a real file, not a preserved symlink -- "
            "symlinks=True was causing silent drops by ignore_non_content"
        )
        assert staged_symlink.read_text(encoding="utf-8") == "# Shared contract\n"

    def test_in_package_nested_symlink_content_preserved(self, tmp_path: Path) -> None:
        """Nested symlinks inside the package are all materialized as real files."""
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        shared = pkg_root / ".apm" / "shared"
        shared.mkdir(parents=True)
        (shared / "base.md").write_text("# Base\n", encoding="utf-8")

        refs = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        _try_symlink(refs / "base.md", Path("../../../shared/base.md"))

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        result = _copy_local_package(
            dep_ref,
            install_path,
            tmp_path,
            project_root=tmp_path,
            logger=NullCommandLogger(),
        )

        assert result is not None
        staged = result / ".apm" / "skills" / "demo-skill" / "references" / "base.md"
        assert staged.exists() and not staged.is_symlink()
        assert staged.read_text(encoding="utf-8") == "# Base\n"


class TestEscapingSymlinkHardFails:
    """Security trap: a symlink whose resolved target escapes the package root
    must HARD-FAIL the install (PathTraversalError), never warn-and-skip."""

    def test_symlink_escaping_package_root_raises(self, tmp_path: Path) -> None:
        """_copy_local_package raises PathTraversalError for an escaping symlink.

        Before the fix, shutil.copytree(..., symlinks=True) silently preserved
        the symlink without validation.  After the fix any symlink resolving
        outside the package root must cause an immediate, actionable failure.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        # File that lives OUTSIDE the package root
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.md"
        secret.write_text("# Sensitive content\n", encoding="utf-8")

        refs = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        # Absolute symlink that escapes pkg_root
        _try_symlink(refs / "evil.md", secret)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        with pytest.raises(PathTraversalError, match=r"(?i)(escape|outside|traversal)"):
            _copy_local_package(
                dep_ref,
                install_path,
                tmp_path,
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )

    def test_escaping_install_path_is_not_partially_created(self, tmp_path: Path) -> None:
        """When an escaping symlink is found the install_path is not left partial.

        The install must either complete fully or fail atomically.  A partial
        tree in apm_modules/ could cause subsequent installs to behave
        incorrectly.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.md"
        secret.write_text("# Sensitive\n", encoding="utf-8")

        refs = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        _try_symlink(refs / "evil.md", secret)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        with pytest.raises(PathTraversalError):
            _copy_local_package(
                dep_ref,
                install_path,
                tmp_path,
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )

        # install_path must NOT contain the attacker's content
        if install_path.exists():
            escaped = install_path / ".apm" / "skills" / "demo-skill" / "references" / "evil.md"
            assert not escaped.exists(), "Attacker content must not be in the staged copy"


class TestNonSymlinkFilesUnchanged:
    """Baseline: regular files are still copied correctly after the fix."""

    def test_regular_files_copied_intact(self, tmp_path: Path) -> None:
        """Non-symlink files are copied with original content after the fix."""
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        result = _copy_local_package(
            dep_ref,
            install_path,
            tmp_path,
            project_root=tmp_path,
            logger=NullCommandLogger(),
        )

        assert result is not None
        assert (result / "apm.yml").exists()
        assert (result / ".apm" / "skills" / "demo-skill" / "SKILL.md").exists()
        assert (result / ".apm" / "skills" / "demo-skill" / "references" / "local.md").read_text(
            encoding="utf-8"
        ) == "# Local reference\n"

    def test_install_path_returned_on_success(self, tmp_path: Path) -> None:
        """_copy_local_package returns install_path when no symlinks are present."""
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        result = _copy_local_package(
            dep_ref,
            install_path,
            tmp_path,
            project_root=tmp_path,
            logger=NullCommandLogger(),
        )

        assert result == install_path


class TestBrokenSymlink:
    """Error path: a broken/dangling symlink must hard-fail with a clear error."""

    def test_broken_symlink_raises_path_traversal_error(self, tmp_path: Path) -> None:
        """_copy_local_package raises PathTraversalError for a dangling symlink.

        A symlink whose target does not exist should fail resolve(strict=True)
        and be caught as a PathTraversalError with a message naming the link
        and the reason (broken/unresolvable target).
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        refs = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        nonexistent = tmp_path / "does-not-exist.md"
        _try_symlink(refs / "broken.md", nonexistent)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        with pytest.raises(PathTraversalError, match=r"(?i)(broken|unresolvable)"):
            _copy_local_package(
                dep_ref,
                install_path,
                tmp_path,
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )


class TestSymlinkToDirectory:
    """In-package symlink-to-directory: the resolved directory tree is copied as real files."""

    def test_in_package_symlink_to_directory_materialized(self, tmp_path: Path) -> None:
        """A symlink pointing to a subdirectory within the package is recursively copied.

        The dest must be a real directory tree (not a symlink) containing
        the resolved directory's contents as regular files.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        shared = pkg_root / ".apm" / "shared"
        shared.mkdir(parents=True)
        (shared / "common.md").write_text("# Common\n", encoding="utf-8")
        (shared / "extra.md").write_text("# Extra\n", encoding="utf-8")

        refs = pkg_root / ".apm" / "skills" / "demo-skill" / "references"
        _try_symlink(refs / "shared-dir", shared)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        result = _copy_local_package(
            dep_ref,
            install_path,
            tmp_path,
            project_root=tmp_path,
            logger=NullCommandLogger(),
        )

        assert result is not None, "_copy_local_package should succeed for in-package dir symlink"

        staged_dir = result / ".apm" / "skills" / "demo-skill" / "references" / "shared-dir"
        assert staged_dir.is_dir(), "Staged path must be a real directory, not a symlink"
        assert not staged_dir.is_symlink(), "Staged path must not be a symlink"
        assert (staged_dir / "common.md").read_text(encoding="utf-8") == "# Common\n"
        assert (staged_dir / "extra.md").read_text(encoding="utf-8") == "# Extra\n"


class TestCircularSymlink:
    """Circular directory symlinks must be detected and hard-fail the install."""

    def test_circular_directory_symlinks_raise_path_traversal_error(self, tmp_path: Path) -> None:
        """_copy_local_package raises PathTraversalError for circular dir symlinks.

        If a symlink-to-directory creates a cycle (dir_a -> dir_b, dir_b -> dir_a)
        the visited-set guard must detect this and abort before hitting any
        OS recursion limit.
        """
        from apm_cli.install.phases.local_content import _copy_local_package

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        _make_valid_package(pkg_root)

        dir_a = pkg_root / ".apm" / "dir_a"
        dir_a.mkdir(parents=True)
        (dir_a / "file.md").write_text("# A\n", encoding="utf-8")

        dir_b = pkg_root / ".apm" / "dir_b"
        dir_b.mkdir(parents=True)
        (dir_b / "file.md").write_text("# B\n", encoding="utf-8")

        # Create a symlink from dir_a/link_to_b -> dir_b (in-package, not escaping)
        # Then from dir_b/link_to_a -> dir_a (creates cycle)
        _try_symlink(dir_a / "link_to_b", dir_b)
        _try_symlink(dir_b / "link_to_a", dir_a)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"
        dep_ref = _make_dep_ref(str(pkg_root))

        with pytest.raises(PathTraversalError, match=r"(?i)(circular|visited)"):
            _copy_local_package(
                dep_ref,
                install_path,
                tmp_path,
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )


class TestUnreadableDirectory:
    """An unreadable package directory must hard-fail with a clear error."""

    def test_permission_error_on_iterdir_raises_path_traversal_error(self, tmp_path: Path) -> None:
        """_copy_tree_dereferencing_validated wraps iterdir OSError.

        If the package directory cannot be listed (e.g. PermissionError), the
        copy must abort with a clear PathTraversalError rather than leaking a
        bare OSError up the install stack.
        """
        from unittest.mock import patch

        from apm_cli.install.phases.local_content import (
            _copy_tree_dereferencing_validated,
        )

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        dst = tmp_path / "dst"

        with patch.object(Path, "iterdir", side_effect=PermissionError("Permission denied")):
            with pytest.raises(PathTraversalError, match=r"Cannot read package directory"):
                _copy_tree_dereferencing_validated(pkg_root, dst, pkg_root)
