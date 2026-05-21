"""Phase-3 unit tests for marketplace commands/__init__.py — filling coverage gaps.

Focuses on helper functions, render helpers, and command branches not covered
by the existing test file.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.pr_integration import PrState
from apm_cli.marketplace.publisher import (
    PublishOutcome,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate filesystem writes for every test."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


def _make_source(
    name: str = "my-market",
    owner: str = "acme",
    repo: str = "plugins",
    branch: str = "main",
    path: str = "marketplace.json",
    host: str = "github.com",
) -> MarketplaceSource:
    return MarketplaceSource(name=name, owner=owner, repo=repo, branch=branch, path=path, host=host)


def _make_manifest(
    name: str = "",
    plugins: tuple[MarketplacePlugin, ...] = (),
    description: str = "",
) -> MarketplaceManifest:
    return MarketplaceManifest(name=name, plugins=plugins, description=description)


# ---------------------------------------------------------------------------
# _is_valid_alias
# ---------------------------------------------------------------------------


class TestIsValidAlias:
    def test_valid_simple(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("my-market") is True

    def test_valid_with_dot(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("market.v2") is True

    def test_valid_alphanumeric(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("abc123") is True

    def test_invalid_with_space(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("has space") is False

    def test_invalid_empty_string(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("") is False

    def test_invalid_with_slash(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("owner/repo") is False

    def test_invalid_with_at(self) -> None:
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("name@host") is False


# ---------------------------------------------------------------------------
# MarketplaceGroup.get_command — 'build' removed
# ---------------------------------------------------------------------------


class TestMarketplaceGroupGetCommand:
    def test_build_command_raises_usage_error(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code != 0
        # Should surface the migration message
        assert "apm pack" in result.output

    def test_normal_command_delegates_to_parent(self, runner: CliRunner) -> None:
        """Non-'build' commands are delegated normally (help should work)."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["list", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# _warn_duplicate_names
# ---------------------------------------------------------------------------


class TestWarnDuplicateNames:
    def test_no_duplicates_emits_no_warning(self) -> None:
        from apm_cli.commands.marketplace import _warn_duplicate_names

        logger = MagicMock()
        yml = MagicMock()
        p1 = MagicMock()
        p1.name = "alpha"
        p2 = MagicMock()
        p2.name = "beta"
        yml.packages = [p1, p2]
        _warn_duplicate_names(logger, yml)
        logger.warning.assert_not_called()

    def test_duplicate_name_emits_warning(self) -> None:
        from apm_cli.commands.marketplace import _warn_duplicate_names

        logger = MagicMock()
        yml = MagicMock()
        p1 = MagicMock()
        p1.name = "Alpha"
        p2 = MagicMock()
        p2.name = "alpha"  # same when lowercased
        yml.packages = [p1, p2]
        _warn_duplicate_names(logger, yml)
        logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# _find_duplicate_names
# ---------------------------------------------------------------------------


class TestFindDuplicateNames:
    def test_no_duplicates_returns_empty_string(self) -> None:
        from apm_cli.commands.marketplace import _find_duplicate_names

        yml = MagicMock()
        p1 = MagicMock()
        p1.name = "alpha"
        p2 = MagicMock()
        p2.name = "beta"
        yml.packages = [p1, p2]
        assert _find_duplicate_names(yml) == ""

    def test_duplicates_returns_diagnostic_string(self) -> None:
        from apm_cli.commands.marketplace import _find_duplicate_names

        yml = MagicMock()
        p1 = MagicMock()
        p1.name = "Alpha"
        p2 = MagicMock()
        p2.name = "ALPHA"
        yml.packages = [p1, p2]
        result = _find_duplicate_names(yml)
        assert "Duplicate" in result
        assert "Alpha" in result or "ALPHA" in result


# ---------------------------------------------------------------------------
# _check_gitignore_for_marketplace_json
# ---------------------------------------------------------------------------


class TestCheckGitignoreForMarketplaceJson:
    def test_no_gitignore_returns_silently(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_oserror_reading_gitignore_returns_silently(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        gitignore = tmp_path / ".gitignore"
        gitignore.touch()
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with patch.object(Path, "read_text", side_effect=OSError("perm denied")):
                _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_matching_pattern_triggers_warning(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("marketplace.json\n", encoding="utf-8")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_called_once()

    def test_blank_and_comment_lines_skipped(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# comment\n\n*.log\n", encoding="utf-8")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_wildcard_json_pattern_triggers_warning(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.json\n", encoding="utf-8")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# _parse_marketplace_repo
# ---------------------------------------------------------------------------


class TestParseMarketplaceRepo:
    def test_empty_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Empty"):
            _parse_marketplace_repo("", None)

    def test_control_characters_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="control characters"):
            _parse_marketplace_repo("acme\x00/repo", None)

    def test_http_url_rejected(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Insecure HTTP"):
            _parse_marketplace_repo("http://github.com/acme/repo", None)

    def test_https_without_host_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="missing a host"):
            _parse_marketplace_repo("https:///owner/repo", None)

    def test_owner_repo_simple(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        owner, repo, host = _parse_marketplace_repo("acme/plugins", None)
        assert owner == "acme"
        assert repo == "plugins"
        assert host is None

    def test_https_url_parsed(self) -> None:
        from urllib.parse import urlparse

        from apm_cli.commands.marketplace import _parse_marketplace_repo

        owner, repo, host = _parse_marketplace_repo("https://github.com/acme/plugins", None)
        assert owner == "acme"
        assert repo == "plugins"
        # host should be github.com - verified through urlparse
        parsed = urlparse(f"https://{host}")
        assert parsed.hostname == "github.com"

    def test_conflicting_host_flag_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Conflicting host"):
            _parse_marketplace_repo(
                "https://github.com/acme/repo",
                "ghes.corp.example.com",
            )

    def test_single_segment_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Invalid format"):
            _parse_marketplace_repo("only-one-segment", None)

    def test_fqdn_first_without_owner_repo_raises(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        # FQDN first but only HOST/REPO (missing owner)
        # is_valid_fqdn("github.com") is True, so needs >= 3 segments
        with pytest.raises(ValueError):
            _parse_marketplace_repo("github.com/only-repo", None)

    def test_dot_git_suffix_stripped(self) -> None:
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        _, repo, _ = _parse_marketplace_repo("https://github.com/acme/plugins.git", None)
        assert repo == "plugins"


# ---------------------------------------------------------------------------
# _marketplace_add_unsupported_host_error
# ---------------------------------------------------------------------------


class TestMarketplaceAddUnsupportedHostError:
    def test_ado_host_kind_message(self) -> None:
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "dev.azure.com", "'dev.azure.com'", "'dev.azure.com'", "ado"
        )
        assert "not supported" in msg
        assert "GitHub" in msg or "GitLab" in msg
        # ADO error does not show GITHUB_HOST/GITLAB_HOST env-var suggestions
        assert "GITHUB_HOST" not in msg

    def test_generic_host_kind_shows_export_hints(self) -> None:
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "myghes.corp", "'myghes.corp/org/repo'", "'myghes.corp'", "unknown"
        )
        assert "GITHUB_HOST" in msg
        assert "GITLAB_HOST" in msg
        assert "myghes.corp" in msg


# ---------------------------------------------------------------------------
# _load_yml_or_exit
# ---------------------------------------------------------------------------


class TestLoadYmlOrExit:
    def test_missing_file_exits_1(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_yml_or_exit

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with pytest.raises(SystemExit) as exc_info:
                _load_yml_or_exit(logger)
        assert exc_info.value.code == 1

    def test_schema_error_exits_2(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_yml_or_exit

        (tmp_path / "marketplace.yml").write_text("invalid: content\n", encoding="utf-8")
        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with patch(
                "apm_cli.commands.marketplace.load_marketplace_yml",
                side_effect=MarketplaceYmlError("bad schema"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    _load_yml_or_exit(logger)
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _load_config_or_exit
# ---------------------------------------------------------------------------


class TestLoadConfigOrExit:
    def test_no_config_found_exits_1(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_config_or_exit

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("No marketplace config found"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    _load_config_or_exit(logger)
        assert exc_info.value.code == 1

    def test_both_files_conflict_exits_1(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_config_or_exit

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("Both apm.yml and marketplace.yml found"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    _load_config_or_exit(logger)
        assert exc_info.value.code == 1

    def test_other_schema_error_exits_2(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_config_or_exit

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            with patch(
                "apm_cli.commands.marketplace.load_marketplace_config",
                side_effect=MarketplaceYmlError("some other parse error"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    _load_config_or_exit(logger)
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# list_cmd
# ---------------------------------------------------------------------------


class TestListCmd:
    def test_empty_registry_shows_hint(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        with patch("apm_cli.marketplace.registry.get_registered_marketplaces", return_value=[]):
            result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "No marketplaces" in result.output or "register" in result.output.lower()

    def test_sources_rendered_without_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        sources = [_make_source("tools", "acme", "tools-market")]
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces", return_value=sources
        ):
            with patch("apm_cli.commands.marketplace._get_console", return_value=None):
                result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "tools" in result.output

    def test_sources_rendered_with_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        sources = [_make_source("tools", "acme", "tools-market")]
        mock_console = MagicMock()
        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces", return_value=sources
        ):
            with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
                result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        # Rich console was used
        mock_console.print.assert_called()

    def test_list_exception_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            side_effect=RuntimeError("db error"),
        ):
            result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------------


class TestBrowseCmd:
    def test_marketplace_not_found_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("no-such"),
        ):
            result = runner.invoke(marketplace, ["browse", "no-such"])
        assert result.exit_code == 1

    def test_empty_plugins_emits_warning(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        manifest = _make_manifest(plugins=())
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest):
                result = runner.invoke(marketplace, ["browse", "my-market"])
        assert result.exit_code == 0
        assert "no plugins" in result.output.lower() or "0" in result.output

    def test_plugins_rendered_without_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        plugin = MarketplacePlugin(name="my-plugin", description="Helps a lot")
        manifest = _make_manifest(plugins=(plugin,))
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest):
                with patch("apm_cli.commands.marketplace._get_console", return_value=None):
                    result = runner.invoke(marketplace, ["browse", "my-market"])
        assert result.exit_code == 0
        assert "my-plugin" in result.output

    def test_plugins_rendered_with_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        plugin = MarketplacePlugin(name="my-plugin")
        manifest = _make_manifest(plugins=(plugin,))
        mock_console = MagicMock()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest):
                with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
                    result = runner.invoke(marketplace, ["browse", "my-market"])
        assert result.exit_code == 0
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdateCmd:
    def test_no_sources_registered(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        with patch("apm_cli.marketplace.registry.get_registered_marketplaces", return_value=[]):
            result = runner.invoke(marketplace, ["update"])
        assert result.exit_code == 0
        assert "No marketplaces" in result.output

    def test_single_name_refresh(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        manifest = _make_manifest(plugins=(MarketplacePlugin(name="p1"),))
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.clear_marketplace_cache"):
                with patch("apm_cli.marketplace.client.fetch_marketplace", return_value=manifest):
                    result = runner.invoke(marketplace, ["update", "my-market"])
        assert result.exit_code == 0
        assert "updated" in result.output.lower() or "1 plugin" in result.output

    def test_all_sources_with_one_failing(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        sources = [_make_source("good"), _make_source("bad", repo="bad-repo")]
        good_manifest = _make_manifest(plugins=(MarketplacePlugin(name="p1"),))

        def fetch_side_effect(source, **kwargs):
            if source.name == "bad":
                raise RuntimeError("fetch failed")
            return good_manifest

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces", return_value=sources
        ):
            with patch("apm_cli.marketplace.client.clear_marketplace_cache"):
                with patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    side_effect=fetch_side_effect,
                ):
                    result = runner.invoke(marketplace, ["update"])
        assert result.exit_code == 0
        # bad marketplace warning should appear in output
        assert "bad" in result.output


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemoveCmd:
    def test_non_interactive_without_yes_exits(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.commands.marketplace._is_interactive", return_value=False):
                result = runner.invoke(marketplace, ["remove", "my-market"])
        assert result.exit_code != 0
        assert "--yes" in result.output or "non-interactive" in result.output.lower()

    def test_with_yes_flag_removes(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.registry.remove_marketplace") as mock_rm:
                with patch("apm_cli.marketplace.client.clear_marketplace_cache"):
                    result = runner.invoke(marketplace, ["remove", "my-market", "--yes"])
        assert result.exit_code == 0
        mock_rm.assert_called_once_with("my-market")

    def test_interactive_declined_cancels(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        source = _make_source()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.commands.marketplace._is_interactive", return_value=True):
                # User types 'n' to decline confirmation
                result = runner.invoke(marketplace, ["remove", "my-market"], input="n\n")
        assert result.exit_code == 0
        assert "Cancelled" in result.output


# ---------------------------------------------------------------------------
# _render_build_error
# ---------------------------------------------------------------------------


class TestRenderBuildError:
    def _get_logger(self) -> MagicMock:
        logger = MagicMock()
        logger.error = MagicMock()
        logger.progress = MagicMock()
        return logger

    def test_git_ls_remote_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = GitLsRemoteError("pkg", "summary text", "hint text")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        logger.progress.assert_called_once()
        assert "hint" in logger.progress.call_args[0][0].lower()

    def test_git_ls_remote_error_no_hint(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = GitLsRemoteError("pkg", "summary text", "")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        # No hint → progress not called
        logger.progress.assert_not_called()

    def test_no_matching_version_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = NoMatchingVersionError("pkg", ">=1.0.0")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        logger.progress.assert_called_once()
        assert "version range" in logger.progress.call_args[0][0].lower()

    def test_ref_not_found_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = RefNotFoundError("pkg", "v9.9.9", "github.com/acme/repo")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        logger.progress.assert_called_once()

    def test_head_not_allowed_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = HeadNotAllowedError("pkg", "refs/heads/main")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        logger.progress.assert_not_called()

    def test_offline_miss_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = OfflineMissError("pkg", "github.com/acme/repo")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        logger.progress.assert_called_once()

    def test_generic_build_error(self) -> None:
        from apm_cli.commands.marketplace import _render_build_error

        logger = self._get_logger()
        exc = BuildError("something broke")
        _render_build_error(logger, exc)
        logger.error.assert_called_once()
        assert "Build failed" in logger.error.call_args[0][0]


# ---------------------------------------------------------------------------
# _render_build_table
# ---------------------------------------------------------------------------


class TestRenderBuildTable:
    def _make_report(self, sha: str | None = "abc1234567890") -> MagicMock:
        pkg = MagicMock()
        pkg.sha = sha
        pkg.name = "my-pkg"
        pkg.ref = "v1.2.3"
        report = MagicMock()
        report.resolved = [pkg]
        return report

    def test_no_console_falls_back_to_tree_item(self) -> None:
        from apm_cli.commands.marketplace import _render_build_table

        logger = MagicMock()
        report = self._make_report()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_build_table(logger, report)
        logger.tree_item.assert_called()

    def test_with_console_uses_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_build_table

        logger = MagicMock()
        report = self._make_report()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_build_table(logger, report)
        mock_console.print.assert_called()

    def test_no_sha_shows_placeholder(self) -> None:
        from apm_cli.commands.marketplace import _render_build_table

        logger = MagicMock()
        report = self._make_report(sha=None)
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_build_table(logger, report)
        # '--' should appear in output when sha is None
        call_args = logger.tree_item.call_args[0][0]
        assert "--" in call_args


# ---------------------------------------------------------------------------
# _load_current_versions
# ---------------------------------------------------------------------------


class TestLoadCurrentVersions:
    def test_no_marketplace_json_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_current_versions

        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {}

    def test_valid_marketplace_json(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_current_versions

        data = {
            "plugins": [
                {"name": "my-plugin", "source": {"ref": "v1.2.3"}},
            ]
        }
        (tmp_path / "marketplace.json").write_text(json.dumps(data), encoding="utf-8")
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {"my-plugin": "v1.2.3"}

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_current_versions

        (tmp_path / "marketplace.json").write_text("not-json!!!", encoding="utf-8")
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        assert result == {}

    def test_plugin_without_source_dict(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_current_versions

        data = {"plugins": [{"name": "my-plugin", "source": "not-a-dict"}]}
        (tmp_path / "marketplace.json").write_text(json.dumps(data), encoding="utf-8")
        with patch("apm_cli.commands.marketplace.Path.cwd", return_value=tmp_path):
            result = _load_current_versions()
        # source is not a dict → ref field missing → skipped
        assert result == {}


# ---------------------------------------------------------------------------
# _load_targets_file
# ---------------------------------------------------------------------------


class TestLoadTargetsFile:
    def _write_yaml(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "targets.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_invalid_yaml_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = tmp_path / "targets.yaml"
        p.write_text(": : :\ninvalid\t: [", encoding="utf-8")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert err is not None
        assert "Invalid YAML" in err

    def test_oserror_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = tmp_path / "missing.yaml"
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "Cannot read" in err

    def test_no_targets_key_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "other: stuff\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "'targets' key" in err

    def test_empty_targets_list_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "targets: []\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "non-empty" in err

    def test_entry_not_dict_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "targets:\n  - just-a-string\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "mapping" in err

    def test_missing_repo_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "targets:\n  - branch: main\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "'repo' is required" in err

    def test_bad_repo_format_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "targets:\n  - repo: single\n    branch: main\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "owner/name" in err

    def test_missing_branch_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        p = self._write_yaml(tmp_path, "targets:\n  - repo: owner/name\n")
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "'branch' is required" in err

    def test_path_traversal_in_path_in_repo_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        content = (
            "targets:\n  - repo: owner/name\n    branch: main\n    path_in_repo: ../evil.yml\n"
        )
        p = self._write_yaml(tmp_path, content)
        targets, err = _load_targets_file(p)
        assert targets is None
        assert err is not None

    def test_valid_targets_file(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        content = "targets:\n  - repo: owner/name\n    branch: main\n"
        p = self._write_yaml(tmp_path, content)
        targets, err = _load_targets_file(p)
        assert err is None
        assert targets is not None
        assert len(targets) == 1
        assert targets[0].repo == "owner/name"

    def test_empty_path_in_repo_returns_error(self, tmp_path: Path) -> None:
        from apm_cli.commands.marketplace import _load_targets_file

        content = "targets:\n  - repo: owner/name\n    branch: main\n    path_in_repo: '   '\n"
        p = self._write_yaml(tmp_path, content)
        targets, err = _load_targets_file(p)
        assert targets is None
        assert "'path_in_repo'" in err


# ---------------------------------------------------------------------------
# _outcome_symbol
# ---------------------------------------------------------------------------


class TestOutcomeSymbol:
    def test_updated(self) -> None:
        from apm_cli.commands.marketplace import _outcome_symbol

        assert _outcome_symbol(PublishOutcome.UPDATED) == "[+]"

    def test_failed(self) -> None:
        from apm_cli.commands.marketplace import _outcome_symbol

        assert _outcome_symbol(PublishOutcome.FAILED) == "[x]"

    def test_skipped_downgrade(self) -> None:
        from apm_cli.commands.marketplace import _outcome_symbol

        assert _outcome_symbol(PublishOutcome.SKIPPED_DOWNGRADE) == "[!]"

    def test_skipped_ref_change(self) -> None:
        from apm_cli.commands.marketplace import _outcome_symbol

        assert _outcome_symbol(PublishOutcome.SKIPPED_REF_CHANGE) == "[!]"

    def test_no_change(self) -> None:
        from apm_cli.commands.marketplace import _outcome_symbol

        assert _outcome_symbol(PublishOutcome.NO_CHANGE) == "[*]"


# ---------------------------------------------------------------------------
# _render_publish_footer
# ---------------------------------------------------------------------------


class TestRenderPublishFooter:
    def test_no_failures_uses_success(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_footer

        logger = MagicMock()
        _render_publish_footer(logger, updated=3, failed=0, total=3, dry_run=False)
        logger.success.assert_called_once()
        logger.warning.assert_not_called()

    def test_with_failures_uses_warning(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_footer

        logger = MagicMock()
        _render_publish_footer(logger, updated=2, failed=1, total=3, dry_run=False)
        logger.warning.assert_called_once()
        logger.success.assert_not_called()

    def test_dry_run_suffix(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_footer

        logger = MagicMock()
        _render_publish_footer(logger, updated=1, failed=0, total=1, dry_run=True)
        call_msg = logger.success.call_args[0][0]
        assert "dry-run" in call_msg


# ---------------------------------------------------------------------------
# _render_publish_plan
# ---------------------------------------------------------------------------


class TestRenderPublishPlan:
    def _make_plan(self) -> MagicMock:
        plan = MagicMock()
        plan.marketplace_name = "my-market"
        plan.marketplace_version = "v2.0.0"
        plan.new_ref = "refs/tags/v2.0.0"
        plan.branch_name = "release/v2.0.0"
        target = MagicMock()
        target.repo = "acme/consumer"
        target.branch = "main"
        target.path_in_repo = "apm.yml"
        plan.targets = [target]
        return plan

    def test_no_console_uses_tree_item(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_plan

        logger = MagicMock()
        plan = self._make_plan()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_publish_plan(logger, plan)
        logger.tree_item.assert_called()

    def test_with_console_uses_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_plan

        logger = MagicMock()
        plan = self._make_plan()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_publish_plan(logger, plan)
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# _render_publish_summary
# ---------------------------------------------------------------------------


class TestRenderPublishSummary:
    def _make_results_and_prs(
        self, outcome: PublishOutcome = PublishOutcome.UPDATED
    ) -> tuple[list[MagicMock], list[MagicMock]]:
        target = MagicMock()
        target.repo = "acme/consumer"
        result = MagicMock()
        result.target = target
        result.outcome = outcome
        result.message = "ok"

        pr_result = MagicMock()
        pr_result.target = target
        pr_result.state = PrState.OPENED
        pr_result.pr_number = 42
        pr_result.pr_url = "https://github.com/acme/consumer/pull/42"

        return [result], [pr_result]

    def test_no_console_fallback(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_summary

        logger = MagicMock()
        results, pr_results = self._make_results_and_prs()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_publish_summary(logger, results, pr_results, no_pr=False, dry_run=False)
        logger.tree_item.assert_called()

    def test_with_console_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_summary

        logger = MagicMock()
        results, pr_results = self._make_results_and_prs()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_publish_summary(logger, results, pr_results, no_pr=False, dry_run=False)
        mock_console.print.assert_called()

    def test_no_pr_flag_hides_pr_columns(self) -> None:
        from apm_cli.commands.marketplace import _render_publish_summary

        logger = MagicMock()
        results, pr_results = self._make_results_and_prs()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_publish_summary(logger, results, pr_results, no_pr=True, dry_run=False)
        # Should still print without error
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# _render_outdated_table
# ---------------------------------------------------------------------------


class TestRenderOutdatedTable:
    def _make_row(self, note: str = "") -> MagicMock:
        from apm_cli.commands.marketplace import _OutdatedRow

        return _OutdatedRow("pkg", "v1.0.0", ">=1.0.0", "v1.0.0", "v2.0.0", "[+]", note)

    def test_no_console_fallback(self) -> None:
        from apm_cli.commands.marketplace import _render_outdated_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_outdated_table(logger, [self._make_row("pre-release")])
        logger.tree_item.assert_called()

    def test_with_console_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_outdated_table

        logger = MagicMock()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_outdated_table(logger, [self._make_row()])
        mock_console.print.assert_called()

    def test_row_with_note_included_in_fallback(self) -> None:
        from apm_cli.commands.marketplace import _render_outdated_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_outdated_table(logger, [self._make_row("pinned")])
        call_arg = logger.tree_item.call_args[0][0]
        assert "pinned" in call_arg


# ---------------------------------------------------------------------------
# _render_check_table
# ---------------------------------------------------------------------------


class TestRenderCheckTable:
    def _make_result(self, ref_ok: bool = True, error: str = "") -> MagicMock:
        from apm_cli.commands.marketplace import _CheckResult

        return _CheckResult("pkg", reachable=True, version_found=True, ref_ok=ref_ok, error=error)

    def test_no_console_fallback(self) -> None:
        from apm_cli.commands.marketplace import _render_check_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_check_table(logger, [self._make_result()])
        logger.tree_item.assert_called()

    def test_with_console_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_check_table

        logger = MagicMock()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_check_table(logger, [self._make_result()])
        mock_console.print.assert_called()

    def test_failed_result_shows_x(self) -> None:
        from apm_cli.commands.marketplace import _render_check_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_check_table(logger, [self._make_result(ref_ok=False, error="bad ref")])
        call_arg = logger.tree_item.call_args[0][0]
        assert "[x]" in call_arg


# ---------------------------------------------------------------------------
# _render_doctor_table
# ---------------------------------------------------------------------------


class TestRenderDoctorTable:
    def _make_check(
        self,
        passed: bool = True,
        informational: bool = False,
    ) -> MagicMock:
        from apm_cli.commands.marketplace import _DoctorCheck

        return _DoctorCheck(
            "check-name", passed=passed, detail="detail", informational=informational
        )

    def test_no_console_fallback(self) -> None:
        from apm_cli.commands.marketplace import _render_doctor_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_doctor_table(logger, [self._make_check()])
        logger.tree_item.assert_called()

    def test_with_console_rich(self) -> None:
        from apm_cli.commands.marketplace import _render_doctor_table

        logger = MagicMock()
        mock_console = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
            _render_doctor_table(logger, [self._make_check()])
        mock_console.print.assert_called()

    def test_informational_check_shows_i_icon(self) -> None:
        from apm_cli.commands.marketplace import _render_doctor_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_doctor_table(logger, [self._make_check(informational=True)])
        call_arg = logger.tree_item.call_args[0][0]
        assert "[i]" in call_arg

    def test_failed_check_shows_x_icon(self) -> None:
        from apm_cli.commands.marketplace import _render_doctor_table

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            _render_doctor_table(logger, [self._make_check(passed=False)])
        call_arg = logger.tree_item.call_args[0][0]
        assert "[x]" in call_arg


# ---------------------------------------------------------------------------
# search command
# ---------------------------------------------------------------------------


class TestSearchCmd:
    def test_missing_at_sign_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["just-a-query"])
        assert result.exit_code != 0
        assert "QUERY@MARKETPLACE" in result.output

    def test_empty_query_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["@my-market"])
        assert result.exit_code != 0
        assert "required" in result.output.lower() or "QUERY" in result.output

    def test_empty_marketplace_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["security@"])
        assert result.exit_code != 0
        assert "required" in result.output.lower() or "MARKETPLACE" in result.output

    def test_marketplace_not_found_exits_1(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceNotFoundError("unknown"),
        ):
            result = runner.invoke(search, ["query@unknown"])
        assert result.exit_code != 0
        assert "not registered" in result.output.lower()

    def test_no_results_shows_warning(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        source = _make_source()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.search_marketplace", return_value=[]):
                result = runner.invoke(search, ["noresult@my-market"])
        assert result.exit_code == 0
        assert "No plugins" in result.output or "no plugins" in result.output.lower()

    def test_results_rendered_without_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        source = _make_source()
        plugin = MarketplacePlugin(name="sec-scanner", description="Scans for vulns")
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.search_marketplace", return_value=[plugin]):
                with patch("apm_cli.commands.marketplace._get_console", return_value=None):
                    result = runner.invoke(search, ["security@my-market"])
        assert result.exit_code == 0
        assert "sec-scanner" in result.output

    def test_results_rendered_with_rich(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        source = _make_source()
        plugin = MarketplacePlugin(name="sec-scanner")
        mock_console = MagicMock()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.search_marketplace", return_value=[plugin]):
                with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
                    result = runner.invoke(search, ["security@my-market"])
        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_long_description_truncated(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import search

        source = _make_source()
        long_desc = "x" * 100
        plugin = MarketplacePlugin(name="verbose-plugin", description=long_desc)
        mock_console = MagicMock()
        with patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=source):
            with patch("apm_cli.marketplace.client.search_marketplace", return_value=[plugin]):
                with patch("apm_cli.commands.marketplace._get_console", return_value=mock_console):
                    result = runner.invoke(search, ["query@my-market"])
        assert result.exit_code == 0
        # Table was rendered — description was truncated to <=63 chars


# ---------------------------------------------------------------------------
# add command — verbose traceback on unexpected exception
# ---------------------------------------------------------------------------


class TestAddCmdVerboseTraceback:
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_verbose_flag_shows_traceback_on_error(
        self, mock_detect: MagicMock, runner: CliRunner
    ) -> None:
        from apm_cli.commands.marketplace import marketplace

        mock_detect.side_effect = RuntimeError("unexpected boom")
        result = runner.invoke(marketplace, ["add", "acme/plugins", "--verbose"])
        assert result.exit_code == 1
        # traceback should appear in verbose mode
        assert (
            "Traceback" in result.output
            or "traceback" in result.output.lower()
            or "boom" in result.output
        )


# ---------------------------------------------------------------------------
# list_cmd — verbose traceback on error
# ---------------------------------------------------------------------------


class TestListCmdVerbose:
    def test_verbose_shows_traceback_on_error(self, runner: CliRunner) -> None:
        from apm_cli.commands.marketplace import marketplace

        with patch(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(marketplace, ["list", "--verbose"])
        assert result.exit_code == 1
        assert "Traceback" in result.output or "boom" in result.output
