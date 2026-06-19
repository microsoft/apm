"""Integration tests for marketplace CLI commands -- phase 3 wave 4.

Covers uncovered lines/branches in:
  src/apm_cli/commands/marketplace/__init__.py

Strategy: hermetic -- uses Click's test runner (CliRunner), mocks external I/O.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import (
    _check_gitignore_for_marketplace_json,
    _find_duplicate_names,
    _is_valid_alias,
    _load_config_or_exit,
    _load_current_versions,
    _load_yml_or_exit,
    _OutdatedRow,
    _parse_marketplace_repo,
    _render_build_error,
    _render_build_table,
    _render_check_table,
    _render_doctor_table,
    _render_outdated_table,
    _warn_duplicate_names,
    marketplace,
    search,
)
from apm_cli.marketplace.errors import (
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)

# ---------------------------------------------------------------------------
# MarketplaceGroup.format_commands -- lines 106-121
# ---------------------------------------------------------------------------


class TestMarketplaceGroupFormatCommands:
    def test_format_commands_skips_none_commands(self):
        """Lines 115-116: skip commands where get_command returns None."""
        runner = CliRunner()
        result = runner.invoke(marketplace, ["--help"])
        assert result.exit_code == 0
        assert "Consumer commands" in result.output

    def test_build_subcommand_raises_usage_error(self):
        """Lines 97-102: 'build' is removed -- error with hint."""
        runner = CliRunner()
        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code != 0
        assert "apm pack" in result.output.lower() or "removed" in result.output.lower()


# ---------------------------------------------------------------------------
# _is_valid_alias
# ---------------------------------------------------------------------------


class TestIsValidAlias:
    def test_empty_string_invalid(self):
        assert _is_valid_alias("") is False

    def test_valid_alias(self):
        assert _is_valid_alias("my-marketplace") is True

    def test_spaces_invalid(self):
        assert _is_valid_alias("my alias") is False

    def test_special_chars_invalid(self):
        assert _is_valid_alias("bad@alias") is False


# ---------------------------------------------------------------------------
# _parse_marketplace_repo
# ---------------------------------------------------------------------------


class TestParseMarketplaceRepo:
    def test_simple_owner_repo(self):
        url, kind, host = _parse_marketplace_repo("owner/repo", None)
        assert url == "https://github.com/owner/repo"
        assert kind == "github"
        assert host == "github.com"

    def test_https_url_parsed(self):
        url, kind, host = _parse_marketplace_repo("https://github.com/owner/my-repo", None)
        assert url == "https://github.com/owner/my-repo"
        assert kind == "github"
        assert host == "github.com"

    def test_http_url_rejected(self):
        with pytest.raises(ValueError, match=r"[Ii]nsecure"):
            _parse_marketplace_repo("http://github.com/owner/repo", None)

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match=r"[Ee]mpty"):
            _parse_marketplace_repo("", None)

    def test_single_segment_rejected(self):
        with pytest.raises(ValueError, match=r"[Ii]nvalid"):
            _parse_marketplace_repo("only-one", None)

    def test_fqdn_prefix_three_segment(self):
        url, kind, host = _parse_marketplace_repo("github.example.com/owner/my-repo", None)
        assert url == "https://github.example.com/owner/my-repo"
        assert kind == "git"
        assert host == "github.example.com"

    def test_fqdn_prefix_only_two_segments_rejected(self):
        """Line 322-326: HOST/REPO without owner is rejected."""
        with pytest.raises(ValueError, match=r"[Ii]nvalid|HOST"):
            _parse_marketplace_repo("github.example.com/repo", None)

    def test_conflicting_host_rejected(self):
        """Line 337-396: embedded host conflicts with --host flag."""
        with pytest.raises(ValueError, match=r"[Cc]onflict|[Mm]ismatch|host"):
            _parse_marketplace_repo("https://github.com/owner/repo", "gitlab.com")

    def test_control_chars_rejected(self):
        with pytest.raises(ValueError, match=r"[Cc]ontrol"):
            _parse_marketplace_repo("owner/\x00repo", None)

    def test_percent_encoded_traversal_rejected(self):
        from apm_cli.utils.path_security import PathTraversalError

        with pytest.raises((PathTraversalError, ValueError)):
            _parse_marketplace_repo("owner/%2E%2E/repo", None)


# ---------------------------------------------------------------------------
# _warn_duplicate_names / _find_duplicate_names -- lines 172-198
# ---------------------------------------------------------------------------


class TestWarnDuplicateNames:
    def _make_yml(self, names: list[str]):
        yml = MagicMock()
        entries = []
        for n in names:
            e = MagicMock()
            e.name = n
            entries.append(e)
        yml.packages = entries
        return yml

    def test_warn_called_for_duplicate(self):
        yml = self._make_yml(["PkgA", "pkga", "PkgB"])
        logger = MagicMock()
        _warn_duplicate_names(logger, yml)
        logger.warning.assert_called()

    def test_no_warning_for_unique_names(self):
        yml = self._make_yml(["alpha", "beta", "gamma"])
        logger = MagicMock()
        _warn_duplicate_names(logger, yml)
        logger.warning.assert_not_called()

    def test_find_duplicates_returns_string(self):
        yml = self._make_yml(["A", "a"])
        result = _find_duplicate_names(yml)
        assert "Duplicate" in result

    def test_find_no_duplicates_returns_empty(self):
        yml = self._make_yml(["X", "Y"])
        assert _find_duplicate_names(yml) == ""


# ---------------------------------------------------------------------------
# _load_yml_or_exit -- lines 124-142
# ---------------------------------------------------------------------------


class TestLoadYmlOrExit:
    def test_missing_file_exits_1(self, tmp_path):
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with pytest.raises(SystemExit) as exc_info:
                _load_yml_or_exit(logger)
        assert exc_info.value.code == 1

    def test_schema_error_exits_2(self, tmp_path):
        (tmp_path / "marketplace.yml").write_text("bad: yaml: {{")
        logger = MagicMock()
        with (
            patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path),
            patch(
                "apm_cli.commands.marketplace.load_marketplace_yml",
                side_effect=MarketplaceYmlError("schema error"),
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _load_yml_or_exit(logger)
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _load_config_or_exit -- lines 145-168
# ---------------------------------------------------------------------------


class TestLoadConfigOrExit:
    def test_missing_config_exits_1(self, tmp_path):
        logger = MagicMock()
        with (
            patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path),
            patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("No marketplace config"),
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _load_config_or_exit(logger)
        assert exc_info.value.code == 1

    def test_both_files_exits_1(self, tmp_path):
        logger = MagicMock()
        with (
            patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path),
            patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("Both apm.yml and marketplace.yml exist"),
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _load_config_or_exit(logger)
        assert exc_info.value.code == 1

    def test_schema_error_exits_2(self, tmp_path):
        logger = MagicMock()
        with (
            patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path),
            patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("validation failed"),
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _load_config_or_exit(logger)
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _check_gitignore_for_marketplace_json -- lines 213-245
# ---------------------------------------------------------------------------


class TestCheckGitignoreForMarketplaceJson:
    def test_no_gitignore_returns_silently(self, tmp_path):
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_gitignore_with_marketplace_json_warns(self, tmp_path):
        (tmp_path / ".gitignore").write_text("marketplace.json\n")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_called()

    def test_gitignore_with_star_json_warns(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.json\n")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_called()

    def test_gitignore_only_comments_no_warning(self, tmp_path):
        (tmp_path / ".gitignore").write_text("# this is a comment\n  \n")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_gitignore_oserror_returns_silently(self, tmp_path):
        (tmp_path / ".gitignore").write_text("test")
        logger = MagicMock()
        with (
            patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.read_text", side_effect=OSError("permission denied")),
        ):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# _load_current_versions -- lines 869-884
# ---------------------------------------------------------------------------


class TestLoadCurrentVersions:
    def test_no_marketplace_json_returns_empty(self, tmp_path):
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {}

    def test_parses_plugins_refs(self, tmp_path):
        data = {
            "plugins": [
                {"name": "pkg-a", "source": {"ref": "v1.2.3"}},
                {"name": "pkg-b", "source": {"ref": "v2.0.0"}},
            ]
        }
        (tmp_path / "marketplace.json").write_text(json.dumps(data))
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {"pkg-a": "v1.2.3", "pkg-b": "v2.0.0"}

    def test_bad_json_returns_empty(self, tmp_path):
        (tmp_path / "marketplace.json").write_text("not json {{")
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {}


# ---------------------------------------------------------------------------
# _render_build_error -- lines 780-805
# ---------------------------------------------------------------------------


class TestRenderBuildError:
    def test_git_ls_remote_error(self):
        logger = MagicMock()
        exc = GitLsRemoteError("pkg", "Remote not reachable", "Check your token")
        _render_build_error(logger, exc)
        logger.error.assert_called()

    def test_git_ls_remote_error_no_hint(self):
        logger = MagicMock()
        exc = GitLsRemoteError("pkg", "Remote not reachable", "")
        exc.hint = None
        _render_build_error(logger, exc)
        logger.error.assert_called()
        # hint not called when None
        calls = [str(c) for c in logger.progress.call_args_list]
        assert not any("Hint" in c for c in calls)

    def test_no_matching_version_error(self):
        logger = MagicMock()
        exc = NoMatchingVersionError("mypkg", ">=1.0.0")
        _render_build_error(logger, exc)
        logger.error.assert_called()
        logger.progress.assert_called()

    def test_ref_not_found_error(self):
        logger = MagicMock()
        exc = RefNotFoundError("mypkg", "v1.2.3", "https://github.com/owner/repo")
        _render_build_error(logger, exc)
        logger.error.assert_called()

    def test_head_not_allowed_error(self):
        logger = MagicMock()
        exc = HeadNotAllowedError("mypkg", "main")
        _render_build_error(logger, exc)
        logger.error.assert_called()

    def test_offline_miss_error(self):
        logger = MagicMock()
        exc = OfflineMissError("mypkg", "https://github.com/owner/repo")
        _render_build_error(logger, exc)
        logger.error.assert_called()

    def test_generic_build_error(self):
        logger = MagicMock()
        exc = Exception("something else")
        _render_build_error(logger, exc)
        logger.error.assert_called()
        assert "Build failed" in str(logger.error.call_args)


# ---------------------------------------------------------------------------
# _render_build_table -- lines 808-843
# ---------------------------------------------------------------------------


class TestRenderBuildTable:
    def _make_pkg(self, name: str, ref: str, sha: str | None = "abcdef1234"):
        pkg = MagicMock()
        pkg.name = name
        pkg.ref = ref
        pkg.sha = sha
        return pkg

    def _make_report(self, pkgs):
        report = MagicMock()
        report.resolved = pkgs
        return report

    def test_no_console_colorama_fallback(self):
        """Lines 812-817: no console -> logger.tree_item fallback."""
        logger = MagicMock()
        report = self._make_report(
            [
                self._make_pkg("pkg-a", "v1.0.0"),
                self._make_pkg("pkg-b", "refs/heads/main", sha=None),
            ]
        )
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_build_table(logger, report)
        assert logger.tree_item.call_count == 2

    def test_with_console_rich_table(self):
        """Lines 819-843: rich table rendered."""
        logger = MagicMock()
        report = self._make_report([self._make_pkg("pkg-a", "v1.0.0")])
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_build_table(logger, report)
        mock_console.print.assert_called()

    def test_branch_ref_labeled_as_ref(self):
        """Lines 837-840: non-semver ref shows as 'ref'."""
        logger = MagicMock()
        # parse_semver returns None for "not-a-version"
        report = self._make_report([self._make_pkg("pkg-a", "not-a-version")])
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_build_table(logger, report)
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# _render_outdated_table -- lines 902-945
# ---------------------------------------------------------------------------


class TestRenderOutdatedTable:
    def _make_row(
        self,
        name: str = "pkg",
        current: str = "v1.0.0",
        note: str | None = None,
    ) -> _OutdatedRow:
        return _OutdatedRow(
            name=name,
            current=current,
            range_spec=">=1.0.0",
            latest_in_range="v1.1.0",
            latest_overall="v2.0.0",
            status="[~]",
            note=note,
        )

    def test_no_console_colorama_fallback(self):
        logger = MagicMock()
        rows = [self._make_row("pkg-a"), self._make_row("pkg-b", note="pre")]
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_outdated_table(logger, rows)
        assert logger.tree_item.call_count == 2

    def test_with_console_rich_table(self):
        logger = MagicMock()
        rows = [self._make_row("pkg")]
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_outdated_table(logger, rows)
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# _render_check_table -- lines 961-1000
# ---------------------------------------------------------------------------


class TestRenderCheckTable:
    def _make_result(self, name: str, ok: bool = True, error: str | None = None):
        r = MagicMock()
        r.name = name
        r.reachable = ok
        r.version_found = ok
        r.ref_ok = ok
        r.error = error
        return r

    def test_no_console_colorama_fallback(self):
        logger = MagicMock()
        results = [
            self._make_result("pkg-a", ok=True),
            self._make_result("pkg-b", ok=False, error="404"),
        ]
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_check_table(logger, results)
        assert logger.tree_item.call_count == 2

    def test_with_console_rich_table(self):
        logger = MagicMock()
        results = [self._make_result("pkg-a")]
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_check_table(logger, results)
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# _render_doctor_table -- lines 1017-1054
# ---------------------------------------------------------------------------


class TestRenderDoctorTable:
    def _make_check(self, name: str, passed: bool = True, informational: bool = False):
        from apm_cli.commands.marketplace import _DoctorCheck

        return _DoctorCheck(name=name, passed=passed, detail="ok", informational=informational)

    def test_no_console_colorama_fallback(self):
        from apm_cli.commands.marketplace import _DoctorCheck

        logger = MagicMock()
        checks = [
            _DoctorCheck("check-a", True, "all good"),
            _DoctorCheck("check-b", False, "failed"),
            _DoctorCheck("check-c", True, "info only", informational=True),
        ]
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_doctor_table(logger, checks)
        assert logger.tree_item.call_count == 3

    def test_with_console_rich_table(self):
        from apm_cli.commands.marketplace import _DoctorCheck

        logger = MagicMock()
        checks = [_DoctorCheck("check-a", True, "ok")]
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_doctor_table(logger, checks)
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestMarketplaceListCommand:
    def test_no_marketplaces_registered(self):
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            # list is a sub-command
            result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0

    def test_with_registered_marketplaces_no_console(self):
        runner = CliRunner()
        mock_source = MagicMock()
        mock_source.name = "my-market"
        mock_source.owner = "owner"
        mock_source.repo = "repo"
        mock_source.branch = "main"
        mock_source.path = "marketplace.json"
        with (
            patch(
                "apm_cli.commands.marketplace.list_cmd.__module__",
                "apm_cli.commands.marketplace",
            ),
            patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[mock_source],
            ),
            patch(
                "apm_cli.commands.marketplace._get_console",
                return_value=None,
            ),
        ):
            result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# marketplace browse command -- lines 631-689
# ---------------------------------------------------------------------------


class TestMarketplaceBrowseCommand:
    def test_browse_exception_exits_1(self):
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("not-found"),
        ):
            result = runner.invoke(marketplace, ["browse", "nonexistent"])
        assert result.exit_code == 1

    def test_browse_no_plugins(self):
        runner = CliRunner()
        mock_source = MagicMock()
        mock_manifest = MagicMock()
        mock_manifest.plugins = []
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=mock_manifest,
            ),
        ):
            result = runner.invoke(marketplace, ["browse", "my-market"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# marketplace update command -- lines 692-735
# ---------------------------------------------------------------------------


class TestMarketplaceUpdateCommand:
    def test_update_named_marketplace(self):
        runner = CliRunner()
        mock_source = MagicMock()
        mock_manifest = MagicMock()
        mock_manifest.plugins = [MagicMock(), MagicMock()]
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.clear_marketplace_cache",
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                return_value=mock_manifest,
            ),
        ):
            result = runner.invoke(marketplace, ["update", "my-market"])
        assert result.exit_code == 0

    def test_update_all_no_registered(self):
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            return_value=[],
        ):
            result = runner.invoke(marketplace, ["update"])
        assert result.exit_code == 0

    def test_update_all_with_source_exception_verbose(self):
        """Line 727-728: individual source exception logged, verbose prints traceback."""
        runner = CliRunner()
        mock_source = MagicMock()
        mock_source.name = "failing-market"
        mock_source.host = "github.com"
        with (
            patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[mock_source],
            ),
            patch(
                "apm_cli.marketplace.client.clear_marketplace_cache",
            ),
            patch(
                "apm_cli.marketplace.client.fetch_marketplace",
                side_effect=RuntimeError("fetch failed"),
            ),
        ):
            result = runner.invoke(marketplace, ["update", "--verbose"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# marketplace remove command -- lines 738-775
# ---------------------------------------------------------------------------


class TestMarketplaceRemoveCommand:
    def test_remove_non_interactive_without_yes_exits_1(self):
        runner = CliRunner()
        mock_source = MagicMock()
        mock_source.name = "my-market"
        mock_source.owner = "owner"
        mock_source.repo = "repo"
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch("apm_cli.commands.marketplace._is_interactive", return_value=False),
        ):
            result = runner.invoke(marketplace, ["remove", "my-market"])
        assert result.exit_code == 1

    def test_remove_with_yes_flag(self):
        runner = CliRunner()
        mock_source = MagicMock()
        mock_source.name = "my-market"
        mock_source.host = "github.com"
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.registry.remove_marketplace",
            ),
            patch(
                "apm_cli.marketplace.client.clear_marketplace_cache",
            ),
        ):
            result = runner.invoke(marketplace, ["remove", "--yes", "my-market"])
        assert result.exit_code == 0

    def test_remove_exception_exits_1(self):
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("not found"),
        ):
            result = runner.invoke(marketplace, ["remove", "--yes", "missing"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# marketplace search command -- lines 1274-1368
# ---------------------------------------------------------------------------


class TestMarketplaceSearchCommand:
    def test_missing_at_sign_exits_1(self):
        runner = CliRunner()
        result = runner.invoke(search, ["query-without-at"])
        assert result.exit_code == 1

    def test_empty_query_exits_1(self):
        runner = CliRunner()
        result = runner.invoke(search, ["@marketplace"])
        assert result.exit_code == 1

    def test_unknown_marketplace_exits_1(self):
        runner = CliRunner()
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("not-found"),
        ):
            result = runner.invoke(search, ["security@unknown"])
        assert result.exit_code == 1

    def test_no_results_warning(self):
        runner = CliRunner()
        mock_source = MagicMock()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.search_marketplace",
                return_value=[],
            ),
        ):
            result = runner.invoke(search, ["nothing@my-market"])
        assert result.exit_code == 0

    def test_results_no_console_colorama_fallback(self):
        runner = CliRunner()
        mock_source = MagicMock()
        plugin = MagicMock()
        plugin.name = "security-scanner"
        plugin.description = "Scans for security issues"
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=mock_source,
            ),
            patch(
                "apm_cli.marketplace.client.search_marketplace",
                return_value=[plugin],
            ),
            patch(
                "apm_cli.commands.marketplace._get_console",
                return_value=None,
            ),
        ):
            result = runner.invoke(search, ["security@my-market"])
        assert result.exit_code == 0
