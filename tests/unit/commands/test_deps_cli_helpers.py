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


def _make_local_dep(local_path=None, repo_url="_local/pkg", version=None):
    """Local dep whose unique key would leak an absolute host slot."""
    dep = MagicMock()
    dep.source = "local"
    dep.local_path = local_path
    dep.repo_url = repo_url
    dep.version = version
    dep.resolved_commit = None
    dep.resolved_ref = None
    # The anchored unique key is an absolute ``local:/...`` slot the display
    # must never surface for a local dep.
    dep.get_unique_key.return_value = "local:/abs/host/path/pkg"
    return dep


class TestDepDisplayNameLocal:
    def test_local_dep_uses_local_path_when_present(self):
        dep = _make_local_dep(local_path="../pkg-depth-2")
        result = _dep_display_name(dep)
        assert result == "../pkg-depth-2@latest"
        assert "local:/" not in result
        dep.get_unique_key.assert_not_called()

    def test_local_dep_falls_back_to_logical_repo_url_when_path_missing(self):
        dep = _make_local_dep(local_path=None, repo_url="_local/pkg-depth-2")
        result = _dep_display_name(dep)
        assert result == "_local/pkg-depth-2@latest"
        assert "local:/" not in result
        dep.get_unique_key.assert_not_called()

    def test_local_dep_with_posix_absolute_path_renders_logical_repo_url(self):
        """A POSIX-absolute declared path must never leak the host filesystem."""
        dep = _make_local_dep(local_path="/Users/alice/pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "/Users/" not in result
        assert result.startswith("_local/")

    def test_local_dep_with_windows_absolute_path_renders_logical_repo_url(self):
        """A Windows-absolute declared path must never leak the host filesystem."""
        dep = _make_local_dep(local_path=r"C:\Users\alice\pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "C:" not in result
        assert "\\" not in result

    def test_local_dep_with_home_prefixed_path_renders_logical_repo_url(self):
        """A ``~``-prefixed declared path embeds home structure; render logical."""
        dep = _make_local_dep(local_path="~/pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "~" not in result


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

    def test_nested_package_manifest_is_not_listed_independently(self, tmp_path):
        """A nested manifest is omitted while its undeclared parent is orphaned."""
        modules_dir = tmp_path / APM_MODULES_DIR
        parent = modules_dir / "_local" / "package"
        nested = parent / "sub-package"
        nested.mkdir(parents=True)
        (parent / APM_YML_FILENAME).write_text("name: package\nversion: 1.0.0\n")
        (nested / APM_YML_FILENAME).write_text("name: sub-package\nversion: 1.0.0\n")

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())

        assert installed is not None
        assert [package["name"] for package in installed] == ["_local/package"]
        assert orphaned == ["_local/package"]


# ---------------------------------------------------------------------------
# _resolve_scope_deps - hash-slot / read-failure leak guards
# ---------------------------------------------------------------------------

_HASH_SLOT = "abcdef123456"


class TestResolveScopeDepsOrphanHashSlot:
    """A genuinely orphaned physical slot (``_local/<12hex>/pkg``) has no
    lockfile entry to map back to. It must still be detected as orphaned, but
    its user-facing name must be the hash-free logical form (``_local/pkg``).
    """

    def test_orphan_hashed_slot_is_orphaned_and_shows_no_hash(self, tmp_path):
        modules_dir = tmp_path / APM_MODULES_DIR
        slot = modules_dir / "_local" / _HASH_SLOT / "pkg"
        slot.mkdir(parents=True)
        (slot / APM_YML_FILENAME).write_text("name: pkg\nversion: 1.0.0\n")
        # No apm.yml declaration and no lockfile -> genuinely orphaned.

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())

        assert installed is not None
        names = [package["name"] for package in installed]
        assert names == ["_local/pkg"]
        assert orphaned == ["_local/pkg"]
        # The raw physical hash slot must never surface in user-facing output.
        for name in names + orphaned:
            assert _HASH_SLOT not in name


class TestResolveScopeDepsReadFailure:
    """A malformed package apm.yml makes ``APMPackage.from_apm_yml`` raise a
    ``ValueError`` embedding the absolute apm.yml path. The read-failure
    warning must not leak that path (or a hash slot); it must identify the
    package by its hash-free logical name and stay actionable.
    """

    def test_malformed_apm_yml_warning_excludes_path_and_hash(self, tmp_path):
        modules_dir = tmp_path / APM_MODULES_DIR
        slot = modules_dir / "_local" / "pkg"
        slot.mkdir(parents=True)
        # Invalid YAML: unbalanced bracket -> yaml.YAMLError -> ValueError(path).
        (slot / APM_YML_FILENAME).write_text("name: [unterminated\nversion: 1.0.0\n")

        logger = _make_logger()
        installed, _orphaned = _resolve_scope_deps(tmp_path, logger)

        assert installed is not None
        warnings = [call.args[0] for call in logger.warning.call_args_list if call.args]
        read_failures = [w for w in warnings if "inspect package" in w or "read package" in w]
        assert read_failures, f"expected a read-failure warning, got: {warnings}"
        message = read_failures[0]
        # Must identify the package by its logical name...
        assert "_local/pkg" in message
        # ...and must NOT leak the absolute apm.yml path or a hash slot.
        assert str(tmp_path) not in message
        assert APM_YML_FILENAME not in message
        assert _HASH_SLOT not in message
