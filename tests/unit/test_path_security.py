"""Tests for path security utilities (CVE path traversal fix).

Covers:
- ensure_path_within() with various traversal payloads
- safe_rmtree() containment enforcement
- Integration with DependencyReference.parse / parse_from_dict / get_install_path
"""

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    safe_rmtree,
    validate_path_segments,
)
from apm_cli.models.dependency import DependencyReference


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


class TestValidatePathSegments:
    """Unit tests for the validate_path_segments utility."""

    def test_accepts_clean_path(self):
        validate_path_segments("owner/repo")

    def test_accepts_single_segment(self):
        validate_path_segments("repo")

    def test_accepts_deep_path(self):
        validate_path_segments("org/project/repo/sub/dir")

    def test_rejects_dotdot(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("owner/../evil")

    def test_rejects_single_dot(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("owner/./repo")

    def test_rejects_leading_dotdot(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("../escape")

    def test_rejects_nested_dotdot(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("a/b/../../c")

    def test_rejects_backslash_dotdot(self):
        """Backslashes are normalised to forward slashes before checking."""
        with pytest.raises(PathTraversalError):
            validate_path_segments("owner\\..\\evil")

    def test_rejects_mixed_separators(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("sub\\..\\..\\esc")

    def test_empty_segments_allowed_by_default(self):
        # Double-slash produces empty segments; allowed unless reject_empty
        validate_path_segments("owner//repo")

    def test_reject_empty_catches_double_slash(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("owner//repo", reject_empty=True)

    def test_reject_empty_catches_trailing_slash(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("owner/repo/", reject_empty=True)

    def test_reject_empty_catches_leading_slash(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("/owner/repo", reject_empty=True)

    def test_reject_empty_passes_clean_path(self):
        validate_path_segments("owner/repo", reject_empty=True)

    def test_context_appears_in_message(self):
        with pytest.raises(PathTraversalError, match="repo_url"):
            validate_path_segments("a/../b", context="repo_url")

    def test_bare_dot_rejected(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments(".")

    def test_bare_dotdot_rejected(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("..")

    def test_allow_current_dir_accepts_dot_segments(self):
        # ./bin/server pattern for shell command call sites
        validate_path_segments("./bin/server", allow_current_dir=True)
        validate_path_segments(".", allow_current_dir=True)
        validate_path_segments("a/./b", allow_current_dir=True)

    def test_allow_current_dir_still_rejects_dotdot(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("../escape", allow_current_dir=True)
        with pytest.raises(PathTraversalError):
            validate_path_segments("a/../b", allow_current_dir=True)

    def test_empty_string_with_reject_empty(self):
        with pytest.raises(PathTraversalError):
            validate_path_segments("", reject_empty=True)


# ---------------------------------------------------------------------------
# DependencyReference parse-time traversal rejection
# ---------------------------------------------------------------------------


class TestDependencyParseTraversalRejection:
    """Verify that parse() and parse_from_dict() reject traversal sequences."""

    def test_parse_rejects_dotdot_in_repo(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse("owner/../evil")

    def test_parse_rejects_dotdot_in_virtual_path(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse("owner/repo/../../etc/passwd")

    def test_parse_rejects_single_dot_segment(self):
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse("owner/./repo")

    def test_parse_from_dict_rejects_dotdot_in_path(self):
        entry = {"git": "https://github.com/owner/repo", "path": "../../etc"}
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict(entry)

    def test_parse_from_dict_rejects_single_dot_in_path(self):
        entry = {"git": "https://github.com/owner/repo", "path": "./hidden"}
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict(entry)

    def test_parse_from_dict_rejects_nested_dotdot(self):
        entry = {
            "git": "https://github.com/owner/repo",
            "path": "subdir/../../escape",
        }
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict(entry)

    def test_parse_from_dict_rejects_backslash_traversal(self):
        """Windows-style backslash traversal must be caught."""
        entry = {"git": "https://github.com/owner/repo", "path": "..\\..\\etc"}
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict(entry)

    def test_parse_from_dict_rejects_mixed_separator_traversal(self):
        entry = {"git": "https://github.com/owner/repo", "path": "sub\\..\\..\\esc"}
        with pytest.raises((ValueError, PathTraversalError)):
            DependencyReference.parse_from_dict(entry)

    def test_parse_from_dict_accepts_valid_subpath(self):
        entry = {"git": "https://github.com/owner/repo", "path": "skills/my-skill"}
        dep = DependencyReference.parse_from_dict(entry)
        assert dep.virtual_path == "skills/my-skill"
        assert dep.is_virtual is True

    def test_parse_accepts_normal_virtual_package(self):
        dep = DependencyReference.parse("owner/repo/prompts/my-file.prompt.md")
        assert dep.is_virtual is True

    # --- SSH URL traversal rejection ---

    def test_ssh_parse_rejects_dotdot_in_repo(self):
        """SSH URLs with '..' traversal in the repo path must be rejected."""
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner/../evil")

    def test_ssh_parse_rejects_nested_dotdot(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:org/../../etc/passwd")

    def test_ssh_parse_rejects_single_dot(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner/./repo")

    def test_ssh_parse_accepts_normal_url(self):
        dep = DependencyReference.parse("git@github.com:owner/repo#main")
        assert dep.repo_url == "owner/repo"
        assert dep.reference == "main"

    def test_ssh_parse_accepts_url_with_git_suffix(self):
        dep = DependencyReference.parse("git@gitlab.com:team/project.git#v1.0")
        assert dep.repo_url == "team/project"
        assert dep.reference == "v1.0"

    def test_ssh_parse_rejects_dotdot_with_alias(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner/../evil@my-alias")

    def test_ssh_parse_rejects_dotdot_with_reference(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner/../../etc#main")

    def test_ssh_parse_rejects_double_slash(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner//repo")

    def test_ssh_parse_rejects_trailing_slash(self):
        with pytest.raises(PathTraversalError):
            DependencyReference.parse("git@github.com:owner/repo/")


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
        dep = DependencyReference.parse(
            "https://dev.azure.com/myorg/myproject/_git/myrepo"
        )
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
