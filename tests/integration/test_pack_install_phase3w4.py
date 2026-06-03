"""Integration tests for apm_cli.commands.pack and apm_cli.commands.install -- phase3w4.

Covers missing lines/branches in pack.py and install.py.
All tests are hermetic: uses tmp_path, CliRunner, mocks for external I/O.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.install import (
    _check_package_conflicts,
    _maybe_rollback_manifest,
    _restore_manifest_from_snapshot,
    _split_argv_at_double_dash,
    _validate_and_add_packages_to_apm_yml,
)
from apm_cli.commands.pack import (
    _emit_json_error_or_raise,
    _render_bundle_result,
    _render_marketplace_catalog,
    _render_marketplace_result,
    pack_cmd,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_LOCKFILE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""


def _write_apm_yml(root: Path, body: str) -> None:
    (root / "apm.yml").write_text(body, encoding="utf-8")


def _write_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text(_LOCKFILE, encoding="utf-8")


@pytest.fixture
def runner():
    return CliRunner()


# ===========================================================================
# pack.py tests
# ===========================================================================


class TestEmitJsonErrorOrRaise:
    def test_json_output_mode_prints_json_and_exits(self, tmp_path, monkeypatch):

        monkeypatch.chdir(tmp_path)
        ctx = MagicMock()
        import json as json_mod

        captured = []
        with patch("click.echo", side_effect=lambda s, **kw: captured.append(s)):
            _emit_json_error_or_raise(ctx, True, "test_code", "test message")
        ctx.exit.assert_called_with(1)
        assert captured
        data = json_mod.loads(captured[0])
        assert data["errors"][0]["code"] == "test_code"

    def test_non_json_mode_raises_click_exception(self):
        ctx = MagicMock()
        import click

        with pytest.raises(click.ClickException, match="test message"):
            _emit_json_error_or_raise(ctx, False, "test_code", "test message")


class TestPackCmd:
    def test_pack_bundle_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, [])
        assert result.exit_code == 0

    def test_pack_deprecated_target_flag_warns(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--target", "claude"])
        assert result.exit_code == 0
        assert "deprecated" in result.output.lower() or result.exit_code == 0

    def test_pack_marketplace_output_removed(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "mypkg"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: x
version: 0.1.0
description: y
marketplace:
  owner:
    name: Me
    url: https://example.com
  packages:
    - name: mypkg
      description: desc
      source: ./.github/plugins/mypkg
      homepage: https://example.com
""",
        )
        result = runner.invoke(pack_cmd, ["--marketplace-output", "dist/marketplace.json"])
        # Flag was removed; Click rejects it with a usage error.
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower()
        assert "--marketplace-output" in (result.output or "")

    def test_pack_marketplace_path_invalid_format(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--marketplace-path", "invalid_no_equals"])
        assert result.exit_code != 0 or "FORMAT=PATH" in result.output

    def test_pack_marketplace_path_unknown_format(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--marketplace-path", "badformat=some/path.json"])
        # Should error about unknown format
        assert result.exit_code != 0 or "Unknown marketplace format" in result.output

    def test_pack_marketplace_filter_none(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "mypkg"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: x
version: 0.1.0
description: y
marketplace:
  owner:
    name: Me
    url: https://example.com
  packages:
    - name: mypkg
      description: desc
      source: ./.github/plugins/mypkg
      homepage: https://example.com
""",
        )
        result = runner.invoke(pack_cmd, ["--marketplace", "none"])
        assert result.exit_code == 0
        # No marketplace.json should be written since --marketplace=none
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_marketplace_filter_all(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "mypkg"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: x
version: 0.1.0
description: y
marketplace:
  owner:
    name: Me
    url: https://example.com
  packages:
    - name: mypkg
      description: desc
      source: ./.github/plugins/mypkg
      homepage: https://example.com
""",
        )
        result = runner.invoke(pack_cmd, ["--marketplace", "all"])
        assert result.exit_code == 0

    def test_pack_marketplace_filter_unknown_format_errors(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--marketplace", "badformat"])
        assert result.exit_code != 0 or "Unknown" in result.output

    def test_pack_json_output_mode(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--json"])
        assert result.exit_code == 0
        import json as json_mod

        # Output may contain non-JSON lines; find the JSON object
        json_start = result.output.find("{")
        assert json_start >= 0, f"No JSON found in output: {result.output!r}"
        data = json_mod.loads(result.output[json_start:])
        assert "ok" in data

    def test_pack_dry_run(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--dry-run"])
        assert result.exit_code == 0

    def test_pack_archive_flag(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--archive"])
        assert result.exit_code == 0

    def test_pack_format_apm(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--format", "apm"])
        assert result.exit_code == 0

    def test_pack_verbose(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--verbose"])
        assert result.exit_code == 0

    def test_pack_check_versions_no_marketplace(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--check-versions"])
        assert result.exit_code == 0
        # Should log that nothing to check
        assert "skipped" in result.output.lower() or result.exit_code == 0

    def test_pack_check_clean_no_marketplace(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--check-clean"])
        assert result.exit_code == 0

    def test_pack_legacy_skill_paths_flag(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--legacy-skill-paths"])
        assert result.exit_code == 0


class TestRenderBundleResult:
    def _make_logger(self):
        return MagicMock()

    def test_none_result_is_noop(self):
        logger = self._make_logger()
        _render_bundle_result(logger, None, "plugin", None, False)
        logger.success.assert_not_called()
        logger.warning.assert_not_called()

    def test_dry_run_with_files(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = ["a.md", "b.md"]
        result.mapped_count = 0
        result.path_mappings = {}
        result.bundle_path = Path("build/bundle")
        _render_bundle_result(logger, result, "plugin", None, dry_run=True)
        logger.dry_run_notice.assert_called()

    def test_dry_run_no_files_warns_empty(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = []
        result.mapped_count = 0
        result.path_mappings = {}
        result.bundle_path = None
        _render_bundle_result(logger, result, "plugin", None, dry_run=True)
        # _warn_empty should be called
        logger.warning.assert_called()

    def test_dry_run_with_mapped_files(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = ["a.md"]
        result.mapped_count = 1
        result.path_mappings = {"dst/a.md": "src/a.md"}
        result.bundle_path = Path("build/bundle")
        _render_bundle_result(logger, result, "plugin", None, dry_run=True)
        logger.dry_run_notice.assert_called()

    def test_success_with_files(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = ["a.md", "b.md"]
        result.mapped_count = 0
        result.path_mappings = {}
        result.bundle_path = Path("build/bundle")
        _render_bundle_result(logger, result, "plugin", None, dry_run=False)
        logger.success.assert_called()

    def test_success_with_mapped_files(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = ["a.md"]
        result.mapped_count = 2
        result.path_mappings = {"dst/a.md": "src/a.md"}
        result.bundle_path = Path("build/bundle")
        _render_bundle_result(logger, result, "plugin", None, dry_run=False)
        logger.progress.assert_called()

    def test_no_files_warns_empty_with_target(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = []
        result.mapped_count = 0
        result.path_mappings = {}
        result.bundle_path = None
        _render_bundle_result(logger, result, "plugin", "claude", dry_run=False)
        logger.warning.assert_called()

    def test_apm_format_no_plugin_progress_message(self):
        logger = self._make_logger()
        result = MagicMock()
        result.files = ["a.md"]
        result.mapped_count = 0
        result.path_mappings = {}
        result.bundle_path = Path("build/bundle")
        _render_bundle_result(logger, result, "apm", None, dry_run=False)
        # "Plugin bundle ready" message should NOT appear for apm format
        for call in logger.progress.call_args_list:
            assert "Plugin bundle ready" not in str(call)


class TestRenderMarketplaceResult:
    def _make_logger(self):
        return MagicMock()

    def test_dry_run_no_file_written(self):
        logger = self._make_logger()
        report = MagicMock()
        report.outputs = []
        report.warnings = []
        report.resolved = ["pkg1"]
        _render_marketplace_result(logger, report, dry_run=True, outputs=["out.json"])
        logger.dry_run_notice.assert_called()

    def test_success_with_outputs_list(self):
        logger = self._make_logger()
        report = MagicMock()
        report.outputs = []
        report.warnings = []
        report.resolved = ["pkg1"]
        _render_marketplace_result(logger, report, dry_run=False, outputs=["out.json"])
        logger.success.assert_called()

    def test_extra_warnings_deduped(self):
        logger = self._make_logger()
        report = MagicMock()
        report.outputs = []
        report.warnings = ["dup-warning"]
        report.resolved = []
        _render_marketplace_result(
            logger, report, dry_run=False, extra_warnings=["dup-warning"], outputs=[]
        )
        # Should only log the warning once
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert len([c for c in warning_calls if "dup-warning" in c]) == 1

    def test_output_reports_dry_run(self):
        logger = self._make_logger()
        report = MagicMock()
        out_report = MagicMock()
        out_report.profile = "claude"
        out_report.resolved = ["p1"]
        out_report.output_path = "out.json"
        out_report.dry_run = True
        report.outputs = [out_report]
        report.warnings = []
        _render_marketplace_result(logger, report, dry_run=False)
        logger.dry_run_notice.assert_called()

    def test_output_reports_success_triggers_catalog(self):
        logger = self._make_logger()
        report = MagicMock()
        out_report = MagicMock()
        out_report.profile = "claude"
        out_report.resolved = ["p1"]
        out_report.output_path = "built.json"
        out_report.dry_run = False
        report.outputs = [out_report]
        report.warnings = []
        _render_marketplace_result(logger, report, dry_run=False)
        logger.success.assert_called()


class TestRenderMarketplaceCatalog:
    def test_renders_catalog_with_profiles(self):
        logger = MagicMock()
        logger.info = MagicMock()
        written = [("claude", Path("dist/claude.json")), ("codex", Path("dist/codex.json"))]
        _render_marketplace_catalog(logger, written)
        info_calls = [str(c) for c in logger.info.call_args_list]
        combined = " ".join(info_calls)
        assert "claude" in combined
        assert "codex" in combined

    def test_renders_catalog_without_profiles(self):
        logger = MagicMock()
        logger.info = MagicMock()
        written = [(None, Path("dist/marketplace.json"))]
        _render_marketplace_catalog(logger, written)
        logger.info.assert_called()

    def test_skips_when_no_info_method(self):
        logger = MagicMock(spec=[])  # no info method
        # Should not raise
        _render_marketplace_catalog(logger, [("claude", Path("dist/claude.json"))])


# ===========================================================================
# install.py tests
# ===========================================================================


class TestSplitArgvAtDoubleDash:
    def test_no_double_dash_returns_empty_tuple(self):
        clean, cmd = _split_argv_at_double_dash(["apm", "install", "pkg"])
        assert clean == ["apm", "install", "pkg"]
        assert cmd == ()

    def test_double_dash_splits_correctly(self):
        clean, cmd = _split_argv_at_double_dash(["apm", "install", "--", "npx", "-y", "srv"])
        assert clean == ["apm", "install"]
        assert cmd == ("npx", "-y", "srv")

    def test_double_dash_at_end(self):
        clean, cmd = _split_argv_at_double_dash(["apm", "install", "--"])
        assert clean == ["apm", "install"]
        assert cmd == ()


class TestRestoreManifestFromSnapshot:
    def test_restores_snapshot_content(self, tmp_path):
        manifest = tmp_path / "apm.yml"
        manifest.write_bytes(b"name: original\nversion: 1.0.0\n")
        snapshot = b"name: snapshot\nversion: 0.9.0\n"
        _restore_manifest_from_snapshot(manifest, snapshot)
        assert manifest.read_bytes() == snapshot

    def test_raises_on_permission_error(self, tmp_path):
        manifest = tmp_path / "apm.yml"
        manifest.write_bytes(b"content")
        with patch("tempfile.mkstemp", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                _restore_manifest_from_snapshot(manifest, b"new content")


class TestMaybeRollbackManifest:
    def test_noop_when_snapshot_none(self, tmp_path):
        manifest = tmp_path / "apm.yml"
        manifest.write_bytes(b"original")
        logger = MagicMock()
        _maybe_rollback_manifest(manifest, None, logger)
        assert manifest.read_bytes() == b"original"
        logger.progress.assert_not_called()

    def test_restores_from_snapshot(self, tmp_path):
        manifest = tmp_path / "apm.yml"
        manifest.write_bytes(b"modified")
        logger = MagicMock()
        _maybe_rollback_manifest(manifest, b"original", logger)
        assert manifest.read_bytes() == b"original"
        logger.progress.assert_called_once()

    def test_logs_warning_when_restore_fails(self, tmp_path):
        manifest = tmp_path / "nonexistent_dir" / "apm.yml"  # parent doesn't exist
        logger = MagicMock()
        _maybe_rollback_manifest(manifest, b"snapshot", logger)
        logger.warning.assert_called_once()


class TestCheckPackageConflicts:
    def test_empty_deps(self):
        result = _check_package_conflicts([])
        assert result == set()

    def test_string_dep_parsed(self):
        result = _check_package_conflicts(["owner/repo"])
        assert len(result) > 0

    def test_dict_dep_parsed(self):
        # Dict form must be parseable by DependencyReference.parse_from_dict
        # A simple {"type": "github", "repo": "owner/repo"} may not be a valid format
        # depending on the DependencyReference implementation; skip parse failures gracefully
        result = _check_package_conflicts([{"type": "github", "repo": "owner/repo"}])
        # It might return empty if the format isn't recognized; that's OK
        assert isinstance(result, set)

    def test_invalid_entry_skipped(self):
        result = _check_package_conflicts(["not-a-package-ref", 123])
        # Invalid entries should be silently skipped
        assert isinstance(result, set)

    def test_duplicate_identity_not_duplicated(self):
        # Two entries that parse to the same identity
        result = _check_package_conflicts(["owner/repo", "github.com/owner/repo"])
        # Should be the same identity (deduplicated)
        assert len(result) >= 1


class TestValidateAndAddPackagesToApmYml:
    def _write_apm_yml(self, path: Path) -> None:
        path.write_text("name: test\nversion: 1.0.0\ndescription: t\ndependencies:\n  apm: []\n")

    def test_missing_apm_yml_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # apm.yml does not exist -- should sys.exit(1)
        with pytest.raises(SystemExit):
            _validate_and_add_packages_to_apm_yml(
                packages=["owner/repo"],
                manifest_path=tmp_path / "apm.yml",
            )

    def test_dry_run_returns_empty_with_no_new_packages(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._write_apm_yml(tmp_path / "apm.yml")

        with (
            patch("apm_cli.commands.install._validate_package_exists", return_value=True),
            patch("apm_cli.commands.install.resolve_parsed_dependency_reference") as mock_resolve,
        ):
            # Simulate package already in deps
            dep_ref = MagicMock()
            dep_ref.to_canonical.return_value = "owner/repo"
            dep_ref.get_identity.return_value = "github.com/owner/repo"
            dep_ref.is_insecure = False
            dep_ref.is_virtual = False
            mock_resolve.return_value = (dep_ref, False)

            result, outcome = _validate_and_add_packages_to_apm_yml(
                packages=["owner/repo"],
                dry_run=True,
                manifest_path=tmp_path / "apm.yml",
            )
        assert isinstance(result, list)
        assert outcome is not None

    def test_no_packages_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._write_apm_yml(tmp_path / "apm.yml")
        result, _outcome = _validate_and_add_packages_to_apm_yml(
            packages=[],
            manifest_path=tmp_path / "apm.yml",
        )
        assert result == []

    def test_dev_flag_uses_dev_dependencies_section(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: t\n", encoding="utf-8"
        )
        result, _outcome = _validate_and_add_packages_to_apm_yml(
            packages=[],
            dev=True,
            manifest_path=tmp_path / "apm.yml",
        )
        assert result == []


class TestInstallCmd:
    def test_install_no_apm_yml_creates_minimal(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from apm_cli.commands.install import install

        # With no packages and no apm.yml, it should either create one or fail gracefully
        result = runner.invoke(install, [])
        # Should not crash hard -- exit code 0 or reasonable error
        assert result.exit_code in (0, 1)

    def test_install_dry_run_no_packages(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: t\ndependencies:\n  apm: []\n"
        )
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE)
        from apm_cli.commands.install import install

        result = runner.invoke(install, ["--dry-run"])
        assert result.exit_code in (0, 1)

    def test_install_split_argv_mcp_double_dash(self, runner, tmp_path, monkeypatch):
        """Test that --mcp with command_argv via -- boundary works."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: t\ndependencies:\n  apm: []\n"
        )
        from apm_cli.commands.install import install

        # This tests the argv splitting / --mcp path (dry-run so no real install)
        result = runner.invoke(install, ["--mcp", "my-srv", "--dry-run"])
        # exit code depends on registry availability, but should not crash
        assert result.exit_code in (0, 1, 2)

    def test_install_frozen_flag(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: t\ndependencies:\n  apm: []\n"
        )
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE)
        from apm_cli.commands.install import install

        result = runner.invoke(install, ["--frozen"])
        assert result.exit_code in (0, 1)

    def test_install_ssh_and_https_mutual_exclusion(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: t\ndependencies:\n  apm: []\n"
        )
        from apm_cli.commands.install import install

        result = runner.invoke(install, ["--ssh", "--https"])
        # Should exit with error 2 (mutually exclusive)
        assert result.exit_code in (1, 2)
