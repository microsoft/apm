"""Unit tests for ``apm_cli.commands.approve`` (apm approve / apm deny).

Covers:
- ``approve_cmd``: no args error, --pending flag, --all flag, named packages
- ``deny_cmd``: exact match, prefix match, not found
- ``_find_matching_key``: exact and prefix matching
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from apm_cli.commands.approve import (
    _find_matching_key,
    approve_cmd,
    deny_cmd,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmpdir: str, extra: dict | None = None) -> Path:
    """Write a minimal apm.yml and return its path."""
    data = {"name": "test-project", "version": "1.0"}
    if extra:
        data.update(extra)
    manifest = Path(tmpdir) / "apm.yml"
    manifest.write_text(yaml.dump(data))
    return manifest


def _write_user_approvals(approvals_path: Path, data: dict) -> None:
    """Write approval data to the given (mocked) user approvals file."""
    approvals_path.parent.mkdir(parents=True, exist_ok=True)
    approvals_path.write_text(yaml.dump(data))


def _create_pkg_with_hooks(apm_modules: Path, name: str) -> None:
    """Create a package directory with a hook file."""
    pkg_dir = apm_modules / name
    hook_dir = pkg_dir / ".apm" / "hooks"
    hook_dir.mkdir(parents=True)
    (hook_dir / "pre-tool-use.json").write_text("{}")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "1.0"}))


def _create_pkg_with_bin(apm_modules: Path, name: str) -> None:
    """Create a package directory with bin/ executables."""
    pkg_dir = apm_modules / name
    bin_dir = pkg_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text("#!/bin/sh")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "2.0"}))


# ---------------------------------------------------------------------------
# _find_matching_key
# ---------------------------------------------------------------------------


class TestFindMatchingKey:
    """Tests for _find_matching_key prefix/exact matching."""

    def test_exact_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo#v1.0") == "owner/repo#v1.0"

    def test_prefix_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") == "owner/repo#v1.0"

    def test_no_match(self) -> None:
        allow = {"other/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") is None

    def test_empty_dict(self) -> None:
        assert _find_matching_key({}, "anything") is None


# ---------------------------------------------------------------------------
# approve_cmd
# ---------------------------------------------------------------------------


class TestApproveCmd:
    """Tests for the apm approve CLI command."""

    def test_no_manifest_exits_1(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(approve_cmd, [])
            assert result.exit_code != 0

    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, [])
            assert result.exit_code != 0
            assert "Specify at least one package" in result.output

    def test_pending_no_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "approved" in result.output.lower()

    def test_pending_with_unapproved_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            apm_modules = Path("apm_modules")
            _create_pkg_with_hooks(apm_modules, "hook-pkg")

            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "hook-pkg" in result.output

    def test_approve_all_writes_user_file(self, tmp_path: Path) -> None:
        """apm approve --all must write to ~/.apm/approvals.yml, not project apm.yml."""
        approvals_file = tmp_path / ".apm" / "approvals.yml"
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            apm_modules = Path("apm_modules")
            _create_pkg_with_hooks(apm_modules, "hook-pkg")
            _create_pkg_with_bin(apm_modules, "bin-pkg")

            with patch(
                "apm_cli.security.executables.get_user_approvals_path",
                return_value=approvals_file,
            ):
                result = runner.invoke(approve_cmd, ["--all"])

            assert result.exit_code == 0
            assert "Approved" in result.output

            # Verify written to user-local file, NOT project apm.yml
            from apm_cli.utils.yaml_io import load_yaml

            project_data = load_yaml(Path("apm.yml"))
            assert "allowExecutables" not in project_data

            assert approvals_file.is_file()
            user_data = load_yaml(approvals_file)
            assert isinstance(user_data, dict)
            assert len(user_data) > 0

    def test_approve_specific_package_writes_user_file(self, tmp_path: Path) -> None:
        approvals_file = tmp_path / ".apm" / "approvals.yml"
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            apm_modules = Path("apm_modules")
            _create_pkg_with_hooks(apm_modules, "hook-pkg")

            with patch(
                "apm_cli.security.executables.get_user_approvals_path",
                return_value=approvals_file,
            ):
                result = runner.invoke(approve_cmd, ["hook-pkg"])

            assert result.exit_code == 0
            assert "Approved" in result.output
            assert approvals_file.is_file()

    def test_approve_unknown_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()

            result = runner.invoke(approve_cmd, ["nonexistent"])
            assert result.exit_code == 0
            assert "not found" in result.output


# ---------------------------------------------------------------------------
# deny_cmd
# ---------------------------------------------------------------------------


class TestDenyCmd:
    """Tests for the apm deny CLI command."""

    def test_deny_existing_entry(self, tmp_path: Path) -> None:
        """apm deny must remove from user file, not project apm.yml."""
        approvals_file = tmp_path / ".apm" / "approvals.yml"
        _write_user_approvals(approvals_file, {"pkg#1.0": {"hooks": True}})

        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")

            with patch(
                "apm_cli.security.executables.get_user_approvals_path",
                return_value=approvals_file,
            ):
                result = runner.invoke(deny_cmd, ["pkg#1.0"])

            assert result.exit_code == 0
            assert "Revoked" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            user_data = load_yaml(approvals_file) or {}
            assert "pkg#1.0" not in user_data

    def test_deny_prefix_match(self, tmp_path: Path) -> None:
        approvals_file = tmp_path / ".apm" / "approvals.yml"
        _write_user_approvals(approvals_file, {"owner/repo#v1.0": {"hooks": True}})

        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")

            with patch(
                "apm_cli.security.executables.get_user_approvals_path",
                return_value=approvals_file,
            ):
                result = runner.invoke(deny_cmd, ["owner/repo"])

            assert result.exit_code == 0
            assert "Revoked" in result.output

    def test_deny_not_found(self, tmp_path: Path) -> None:
        approvals_file = tmp_path / ".apm" / "approvals.yml"
        _write_user_approvals(approvals_file, {})

        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")

            with patch(
                "apm_cli.security.executables.get_user_approvals_path",
                return_value=approvals_file,
            ):
                result = runner.invoke(deny_cmd, ["nonexistent"])

            assert result.exit_code == 0
            assert "not found" in result.output
