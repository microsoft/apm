"""Integration tests for apm view command coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.view import (
    _lookup_lockfile_ref,
    resolve_package_path,
)
from apm_cli.core.command_logger import CommandLogger


class TestResolvePackagePath:
    """Tests for resolve_package_path helper."""

    def test_resolve_direct_match_with_apm_yml(self, tmp_path: Path):
        """Resolve package with direct apm.yml match."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        org_dir = apm_modules / "owner"
        org_dir.mkdir()
        pkg_dir = org_dir / "repo"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: owner/repo")

        logger = MagicMock(spec=CommandLogger)

        result = resolve_package_path("owner/repo", apm_modules, logger)

        assert result == pkg_dir

    def test_resolve_direct_match_with_skill_md(self, tmp_path: Path):
        """Resolve package with direct SKILL.md match."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        org_dir = apm_modules / "owner"
        org_dir.mkdir()
        pkg_dir = org_dir / "skill-name"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# Skill")

        logger = MagicMock(spec=CommandLogger)

        result = resolve_package_path("owner/skill-name", apm_modules, logger)

        assert result == pkg_dir

    def test_resolve_fallback_short_name(self, tmp_path: Path):
        """Fallback resolution for short (repo-only) names."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        org_dir = apm_modules / "owner"
        org_dir.mkdir()
        pkg_dir = org_dir / "my-repo"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: owner/my-repo")

        logger = MagicMock(spec=CommandLogger)

        result = resolve_package_path("my-repo", apm_modules, logger)

        assert result == pkg_dir

    def test_resolve_path_traversal_attack_in_package_name(self, tmp_path: Path):
        """Reject path traversal sequences in package name."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        logger = MagicMock(spec=CommandLogger)

        result = resolve_package_path("../../../etc/passwd", apm_modules, logger)

        assert result is None

    def test_resolve_not_found_exits(self, tmp_path: Path, capsys):
        """Nonexistent package causes sys.exit(1)."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        logger = MagicMock(spec=CommandLogger)

        with pytest.raises(SystemExit) as exc_info:
            resolve_package_path("nonexistent/package", apm_modules, logger)

        assert exc_info.value.code == 1

    def test_resolve_ignores_hidden_directories(self, tmp_path: Path):
        """Hidden directories (starting with .) are ignored during fallback scan."""
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        # Create hidden directory that should be ignored
        hidden_dir = apm_modules / ".hidden"
        hidden_dir.mkdir()
        hidden_pkg = hidden_dir / "some-package"
        hidden_pkg.mkdir()
        (hidden_pkg / "apm.yml").write_text("name: hidden/some-package")

        # Also create a normal match
        normal_dir = apm_modules / "normal"
        normal_dir.mkdir()
        normal_pkg = normal_dir / "some-package"
        normal_pkg.mkdir()
        (normal_pkg / "apm.yml").write_text("name: normal/some-package")

        logger = MagicMock(spec=CommandLogger)

        result = resolve_package_path("some-package", apm_modules, logger)

        # Should find the normal directory, not the hidden one
        assert result == normal_pkg


class TestLookupLockfileRef:
    """Tests for _lookup_lockfile_ref helper."""

    def test_lookup_exact_key_match(self, tmp_path: Path):
        """Match package by exact lockfile key."""
        mock_dep = MagicMock()
        mock_dep.resolved_ref = "v1.0.0"
        mock_dep.resolved_commit = "abc123def456"

        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"owner/repo": mock_dep}

        with patch("apm_cli.deps.lockfile.get_lockfile_path") as mock_get_path:
            with patch("apm_cli.deps.lockfile.LockFile") as mock_lf_class:
                with patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"):
                    mock_get_path.return_value = tmp_path / "apm.lock.yaml"
                    mock_lf_class.read.return_value = mock_lockfile

                    ref, commit = _lookup_lockfile_ref("owner/repo", tmp_path)

                    assert ref == "v1.0.0"
                    assert commit == "abc123def456"

    def test_lookup_missing_lockfile_returns_empty(self, tmp_path: Path):
        """Missing or unreadable lockfile returns empty strings."""
        with patch("apm_cli.deps.lockfile.get_lockfile_path") as mock_get_path:
            with patch("apm_cli.deps.lockfile.LockFile") as mock_lf_class:
                with patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"):
                    mock_get_path.return_value = tmp_path / "nonexistent.lock.yaml"
                    mock_lf_class.read.return_value = None

                    ref, commit = _lookup_lockfile_ref("owner/repo", tmp_path)

                    assert ref == ""
                    assert commit == ""

    def test_lookup_exception_returns_empty(self, tmp_path: Path):
        """Exceptions during lockfile access return empty strings."""
        with patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed") as mock_migrate:
            mock_migrate.side_effect = Exception("Lockfile error")

            ref, commit = _lookup_lockfile_ref("owner/repo", tmp_path)

            assert ref == ""
            assert commit == ""
