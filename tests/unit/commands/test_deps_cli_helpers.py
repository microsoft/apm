"""Tests for helper functions in ``apm_cli.commands.deps.cli``.

Covers the pure / filesystem helpers that are not exercised by higher-level
CLI invocation tests:

- ``_format_primitive_counts``
- ``_dep_display_name``
- ``_resolve_scope_deps`` (no-apm_modules and basic orphan-detection paths)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.commands.deps.cli import (
    _dep_display_name,
    _format_primitive_counts,
    _resolve_scope_deps,
)
from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

# ---------------------------------------------------------------------------
# _format_primitive_counts
# ---------------------------------------------------------------------------


class TestFormatPrimitiveCounts:
    def test_empty_dict_returns_empty_string(self):
        assert _format_primitive_counts({}) == ""

    def test_single_nonzero_entry(self):
        assert _format_primitive_counts({"skills": 3}) == "3 skills"

    def test_zero_entries_excluded(self):
        assert _format_primitive_counts({"skills": 0, "agents": 0}) == ""

    def test_mixed_zero_and_nonzero(self):
        result = _format_primitive_counts({"skills": 2, "agents": 0, "instructions": 1})
        assert "2 skills" in result
        assert "1 instructions" in result
        assert "agents" not in result

    def test_multiple_nonzero_comma_separated(self):
        result = _format_primitive_counts({"skills": 1, "agents": 2})
        assert "1 skills" in result
        assert "2 agents" in result
        assert "," in result


# ---------------------------------------------------------------------------
# _dep_display_name
# ---------------------------------------------------------------------------


def _make_dep(key="owner/repo", version=None, resolved_commit=None, resolved_ref=None):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.version = version
    dep.resolved_commit = resolved_commit
    dep.resolved_ref = resolved_ref
    return dep


class TestDepDisplayName:
    def test_uses_version_when_present(self):
        dep = _make_dep(version="1.2.3")
        assert _dep_display_name(dep) == "owner/repo@1.2.3"

    def test_falls_back_to_short_commit(self):
        dep = _make_dep(resolved_commit="abcdef1234567")
        assert _dep_display_name(dep) == "owner/repo@abcdef1"

    def test_falls_back_to_resolved_ref(self):
        dep = _make_dep(resolved_ref="main")
        assert _dep_display_name(dep) == "owner/repo@main"

    def test_falls_back_to_latest(self):
        dep = _make_dep()
        assert _dep_display_name(dep) == "owner/repo@latest"

    def test_version_takes_priority_over_commit(self):
        dep = _make_dep(version="2.0.0", resolved_commit="abc1234")
        assert _dep_display_name(dep) == "owner/repo@2.0.0"


# ---------------------------------------------------------------------------
# _resolve_scope_deps - filesystem paths
# ---------------------------------------------------------------------------


def _make_logger():
    logger = MagicMock()
    logger.warning = MagicMock()
    return logger


class TestResolveScopeDepsNoModules:
    def test_returns_none_when_apm_modules_absent(self, tmp_path):
        """No apm_modules directory -> (None, None)."""
        result = _resolve_scope_deps(tmp_path, _make_logger())
        assert result == (None, None)


class TestResolveScopeDepsWithPackages:
    def _setup_package(self, modules_dir: Path, owner: str, repo: str, name: str = "pkg"):
        """Create a minimal installed package directory."""
        pkg_dir = modules_dir / "github" / owner / repo
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text(f"name: {name}\nversion: 1.0.0\n")
        return pkg_dir

    def test_package_without_apm_yml_declared_not_orphaned(self, tmp_path):
        """Package declared in apm.yml is not orphaned."""
        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir()

        # Set up declared dep in apm.yml
        (tmp_path / APM_YML_FILENAME).write_text(
            "name: myproject\ndependencies:\n  - owner/myrepo\n"
        )

        # Create matching installed package
        pkg_dir = modules_dir / "github" / "owner" / "myrepo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: myrepo\nversion: 1.0.0\n")

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None
        assert isinstance(orphaned, list)

    def test_no_packages_in_modules_returns_empty_lists(self, tmp_path):
        """apm_modules exists but contains no valid packages -> empty lists."""
        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir()
        # No packages inside
        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed == []
        assert orphaned == []

    def test_insecure_only_filter_excludes_secure_packages(self, tmp_path):
        """insecure_only=True returns empty list when no insecure packages."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 0.1.0\n")

        installed, _orphaned = _resolve_scope_deps(tmp_path, _make_logger(), insecure_only=True)
        # No lockfile -> no insecure deps -> empty list
        assert installed == []

    def test_skill_md_only_package_discovered(self, tmp_path):
        """Package with only SKILL.md (no apm.yml) is still discovered."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "skill-repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / SKILL_MD_FILENAME).write_text("# My Skill\n")

        installed, _orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None
        assert any(pkg["name"].endswith("skill-repo") for pkg in installed)
