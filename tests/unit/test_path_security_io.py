"""Tests for path security utilities (CVE path traversal fix) (filesystem, process and CLI boundary cases)."""

import shutil  # noqa: F401
from pathlib import Path

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    safe_rmtree,
)

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# ensure_path_within
# ---------------------------------------------------------------------------


class TestEnsurePathWithin:
    """Unit tests for the ensure_path_within containment check."""

    def test_path_inside_base_passes(self, tmp_path):
        child = tmp_path / "apm_modules" / "owner" / "repo"
        child.mkdir(parents=True)
        result = ensure_path_within(child, tmp_path / "apm_modules")
        assert result == child.resolve()

    def test_dotdot_escape_raises(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        bad = base / ".." / "etc"
        with pytest.raises(PathTraversalError):
            ensure_path_within(bad, base)

    def test_deep_dotdot_escape_raises(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        bad = base / "owner" / "repo" / ".." / ".." / ".." / "secrets"
        with pytest.raises(PathTraversalError):
            ensure_path_within(bad, base)

    def test_base_itself_passes(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        result = ensure_path_within(base, base)
        assert result == base.resolve()

    def test_absolute_outside_raises(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        with pytest.raises(PathTraversalError):
            ensure_path_within(Path("/tmp/evil"), base)

    def test_symlink_escape_raises(self, tmp_path):
        """Symlink inside base pointing outside should be caught."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        outside = tmp_path / "outside_target"
        outside.mkdir()
        link = base / "evil_link"
        link.symlink_to(outside)
        with pytest.raises(PathTraversalError):
            ensure_path_within(link, base)


# ---------------------------------------------------------------------------
# safe_rmtree
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    """Unit tests for the safe_rmtree wrapper."""

    def test_removes_directory_inside_base(self, tmp_path):
        base = tmp_path / "apm_modules"
        target = base / "owner" / "repo"
        target.mkdir(parents=True)
        (target / "file.txt").write_text("content")

        safe_rmtree(target, base)
        assert not target.exists()

    def test_refuses_to_remove_outside_base(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        outside = tmp_path / "SAFE_DELETE_ME"
        outside.mkdir()
        (outside / "proof.txt").write_text("do not delete")

        bad_path = base / ".." / "SAFE_DELETE_ME"
        with pytest.raises(PathTraversalError):
            safe_rmtree(bad_path, base)

        # Verify the directory was NOT deleted
        assert outside.exists()
        assert (outside / "proof.txt").read_text() == "do not delete"

    def test_refuses_traversal_in_package_name(self, tmp_path):
        """Simulates the MSRC PoC: attacker/repo/../../../SAFE_DELETE_ME."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        victim = tmp_path / "SAFE_DELETE_ME"
        victim.mkdir()
        (victim / "proof.txt").write_text("critical data")

        evil_path = base / "attacker" / "repo" / ".." / ".." / ".." / "SAFE_DELETE_ME"
        with pytest.raises(PathTraversalError):
            safe_rmtree(evil_path, base)

        assert victim.exists()


# ---------------------------------------------------------------------------
# validate_path_segments
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Strict URL-path decoding
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DependencyReference parse-time traversal rejection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DependencyReference.get_install_path containment
# ---------------------------------------------------------------------------


class TestGetInstallPathContainment:
    """Verify get_install_path() rejects paths that escape apm_modules/."""

    def test_normal_package_path(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference.parse("owner/repo")
        path = dep.get_install_path(base)
        assert path == base / "owner" / "repo"

    def test_traversal_in_virtual_path_raises(self, tmp_path):
        """Even if parse-time validation is bypassed, get_install_path catches it."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference(repo_url="owner/repo")
        dep.is_virtual = True
        # Need enough ../s to escape: owner/repo/../../../../esc  → 2 up from subdir + 2 more
        dep.virtual_path = "../../../../etc/passwd"
        with pytest.raises(PathTraversalError):
            dep.get_install_path(base)

    def test_traversal_in_repo_url_raises(self, tmp_path):
        """repo_url with .. should be caught by get_install_path."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference(repo_url="owner/repo/../../..")
        dep.is_virtual = False
        with pytest.raises(PathTraversalError):
            dep.get_install_path(base)

    def test_ado_normal_path(self, tmp_path):
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference.parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        path = dep.get_install_path(base)
        assert "myorg" in str(path)
        assert path.resolve().is_relative_to(base.resolve())

    def test_local_path_dotdot_basename_raises(self, tmp_path):
        """Crafted local_path with '..' basename must be caught."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference(repo_url="unused")
        dep.is_local = True
        dep.local_path = "/some/path/.."
        with pytest.raises(PathTraversalError):
            dep.get_install_path(base)

    def test_local_path_dot_basename_raises(self, tmp_path):
        """Path('.').name returns '' on some platforms — guard handles it."""
        base = tmp_path / "apm_modules"
        base.mkdir()
        dep = DependencyReference(repo_url="unused")
        dep.is_local = True
        dep.local_path = "."
        # Path(".").name is "" which is in our reject set
        with pytest.raises(PathTraversalError):
            dep.get_install_path(base)
