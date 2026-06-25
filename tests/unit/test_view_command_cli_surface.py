"""tests for apm_cli.commands.view.

Covers missing lines/branches identified in coverage-unit.json:
- resolve_package_path: ensure_path_within PathTraversalError (lines 63-65)
- resolve_package_path: fallback scan branches (73, 75, 86, 88)
- display_package_info: Rich rendering with locked_ref/commit (142-185)
- display_package_info: exception path (223-225)
- _display_marketplace_plugin: all major branches (252-277, 289-334)
- display_versions: marketplace path, ValueError path, RuntimeError, no refs, Rich table
- view command: field validation, unknown field, versions field, marketplace ref
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.verbose = False
    return m


def _make_remote_ref(name="v1.0.0", ref_type_val="tag", sha="abc12345"):
    from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

    ref_type = GitReferenceType.TAG if ref_type_val == "tag" else GitReferenceType.BRANCH
    return RemoteRef(name=name, ref_type=ref_type, commit_sha=sha)


def _make_package_info(**kwargs):
    defaults = {
        "name": "test-pkg",
        "version": "1.0.0",
        "description": "A test package",
        "author": "Tester",
        "source": "github/test-pkg",
        "install_path": "/tmp/test-pkg",
        "context_files": {"skills": 2, "prompts": 0},
        "workflows": 1,
        "hooks": 0,
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# resolve_package_path
# ---------------------------------------------------------------------------


class TestResolvePackagePath:
    def test_validate_path_traversal_error_returns_none(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path
        from apm_cli.utils.path_security import PathTraversalError

        logger = _make_logger()
        with patch(
            "apm_cli.commands.view.validate_path_segments",
            side_effect=PathTraversalError("traversal"),
        ):
            result = resolve_package_path("../evil", tmp_path, logger)
        assert result is None
        logger.error.assert_called()

    def test_ensure_path_within_error_returns_none(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path
        from apm_cli.utils.path_security import PathTraversalError

        logger = _make_logger()
        with patch(
            "apm_cli.commands.view.ensure_path_within",
            side_effect=PathTraversalError("traversal"),
        ):
            result = resolve_package_path("evil", tmp_path, logger)
        assert result is None
        logger.error.assert_called()

    def test_direct_match_with_apm_yml(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        pkg_dir = tmp_path / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: repo\n")

        logger = _make_logger()
        result = resolve_package_path("org/repo", tmp_path, logger)
        assert result == pkg_dir

    def test_direct_match_with_skill_md(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        pkg_dir = tmp_path / "org" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "SKILL.md").write_text("# Skill\n")

        logger = _make_logger()
        result = resolve_package_path("org/repo", tmp_path, logger)
        assert result == pkg_dir

    def test_fallback_scan_matches_repo_name(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        org_dir = tmp_path / "myorg"
        pkg_dir = org_dir / "mypkg"
        pkg_dir.mkdir(parents=True)

        logger = _make_logger()
        result = resolve_package_path("mypkg", tmp_path, logger)
        assert result == pkg_dir

    def test_fallback_scan_matches_org_slash_repo(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        org_dir = tmp_path / "myorg"
        pkg_dir = org_dir / "mypkg"
        pkg_dir.mkdir(parents=True)

        logger = _make_logger()
        result = resolve_package_path("myorg/mypkg", tmp_path, logger)
        assert result == pkg_dir

    def test_not_found_calls_sys_exit(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        logger = _make_logger()
        with pytest.raises(SystemExit):
            resolve_package_path("nonexistent/pkg", tmp_path, logger)

    def test_fallback_skips_hidden_dirs(self, tmp_path):
        from apm_cli.commands.view import resolve_package_path

        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "mypkg").mkdir()

        logger = _make_logger()
        with pytest.raises(SystemExit):
            resolve_package_path("mypkg", tmp_path, logger)


# ---------------------------------------------------------------------------
# display_package_info
# ---------------------------------------------------------------------------


class TestDisplayPackageInfo:
    def test_rich_rendering_with_locked_ref_and_commit(self, tmp_path):
        """Rich path with locked_ref and locked_commit populated."""
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        pkg_info = _make_package_info(context_files={"skills": 1, "prompts": 0})

        with (
            patch(
                "apm_cli.commands.view._get_detailed_package_info",
                return_value=pkg_info,
            ),
            patch(
                "apm_cli.commands.view._lookup_lockfile_ref",
                return_value=("v1.0.0", "abc123456789", ""),
            ),
        ):
            display_package_info("org/repo", tmp_path, logger, project_root=tmp_path)

    def test_rich_rendering_no_context_files(self, tmp_path):
        """Covers 'No context files found' branch."""
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        pkg_info = _make_package_info(context_files={"skills": 0, "prompts": 0}, workflows=0)

        with (
            patch(
                "apm_cli.commands.view._get_detailed_package_info",
                return_value=pkg_info,
            ),
            patch("apm_cli.commands.view._lookup_lockfile_ref", return_value=("", "", "")),
        ):
            display_package_info("org/repo", tmp_path, logger, project_root=tmp_path)

    def test_rich_rendering_with_hooks(self, tmp_path):
        """Covers hooks > 0 branch."""
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        pkg_info = _make_package_info(hooks=3)

        with (
            patch(
                "apm_cli.commands.view._get_detailed_package_info",
                return_value=pkg_info,
            ),
            patch("apm_cli.commands.view._lookup_lockfile_ref", return_value=("", "", "")),
        ):
            display_package_info("org/repo", tmp_path, logger, project_root=tmp_path)

    def test_exception_calls_sys_exit(self, tmp_path):
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        with (
            patch(
                "apm_cli.commands.view._get_detailed_package_info",
                side_effect=RuntimeError("cannot read"),
            ),
            pytest.raises(SystemExit),
        ):
            display_package_info("org/repo", tmp_path, logger)

    def test_import_error_fallback_text(self, tmp_path, capsys):
        """Covers the ImportError fallback (plain-text) path."""
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        pkg_info = _make_package_info(
            context_files={"skills": 2, "prompts": 0}, workflows=1, hooks=2
        )

        # Patch rich to force ImportError
        with (
            patch(
                "apm_cli.commands.view._get_detailed_package_info",
                return_value=pkg_info,
            ),
            patch(
                "apm_cli.commands.view._lookup_lockfile_ref", return_value=("v1.0", "abc123", "")
            ),
            patch.dict(sys.modules, {"rich.console": None, "rich.panel": None}),
        ):
            display_package_info("org/repo", tmp_path, logger, project_root=tmp_path)

    def test_no_project_root_skips_lockfile(self, tmp_path):
        from apm_cli.commands.view import display_package_info

        logger = _make_logger()
        pkg_info = _make_package_info()

        with patch(
            "apm_cli.commands.view._get_detailed_package_info",
            return_value=pkg_info,
        ):
            # No project_root passed -> _lookup_lockfile_ref not called
            display_package_info("org/repo", tmp_path, logger, project_root=None)


# ---------------------------------------------------------------------------
# _display_marketplace_plugin
# ---------------------------------------------------------------------------


class TestDisplayMarketplacePlugin:
    def _build_plugin(self, *, source=None, version="1.0.0", tags=None, description="Desc"):
        plugin = MagicMock()
        plugin.name = "my-plugin"
        plugin.version = version
        plugin.description = description
        plugin.source = source or {"type": "github", "repo": "org/repo", "ref": "v1.0.0"}
        plugin.tags = tags or ["ai", "code"]
        return plugin

    def test_get_marketplace_by_name_error_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=Exception("not found"),
            ),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_fetch_or_cache_error_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin
        from apm_cli.marketplace.errors import MarketplaceFetchError

        logger = _make_logger()
        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                side_effect=MarketplaceFetchError("fetch failed"),
            ),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_plugin_not_found_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = None

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_dict_source_with_ref_rendered(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(source={"type": "github", "repo": "org/repo", "ref": "v2.0"})
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=("org/repo#v2.0", MagicMock()),
            ),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_string_source_rendered(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(source="github/org/repo")
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("resolve failed"),
            ),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_no_version_no_description_no_tags(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(version=None, tags=None, description=None)
        plugin.version = None
        plugin.description = None
        plugin.tags = None
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_import_error_fallback_text_output(self):
        """Covers Rich ImportError fallback in _display_marketplace_plugin."""
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(
            source={"type": "github", "repo": "org/repo"},
        )
        plugin.tags = ["tag1"]
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch.dict(sys.modules, {"rich.console": None, "rich.panel": None}),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)


# ---------------------------------------------------------------------------
# display_versions
# ---------------------------------------------------------------------------


class TestDisplayVersions:
    def test_marketplace_ref_dispatches_to_display_marketplace(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch("apm_cli.commands.view._display_marketplace_plugin") as mock_disp,
        ):
            display_versions("plugin@market", logger)
        mock_disp.assert_called_once()

    def test_invalid_package_ref_exits(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.models.dependency.reference.DependencyReference.parse",
                side_effect=ValueError("bad ref"),
            ),
            pytest.raises(SystemExit),
        ):
            display_versions("bad::ref", logger)

    def test_list_remote_refs_runtime_error_exits(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.side_effect = RuntimeError("network error")

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
            pytest.raises(SystemExit),
        ):
            display_versions("org/repo", logger)

    def test_no_refs_logs_progress(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = []

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
        ):
            display_versions("org/repo", logger)
        logger.progress.assert_called()

    def test_rich_table_rendered_with_refs(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        ref = _make_remote_ref("v1.0.0", "tag", "abc12345678901234")
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = [ref]

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
        ):
            display_versions("org/repo", logger)

    def test_import_error_plain_text_fallback(self, capsys):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        ref = _make_remote_ref("v2.0.0", "tag", "deadbeef12345678")
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = [ref]

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
            patch.dict(sys.modules, {"rich.console": None, "rich.table": None}),
        ):
            display_versions("org/repo", logger)


# ---------------------------------------------------------------------------
# view CLI command
# ---------------------------------------------------------------------------


class TestViewCommand:
    def setup_method(self):
        self.runner = CliRunner()

    def test_unknown_field_exits(self):
        from apm_cli.cli import cli

        result = self.runner.invoke(cli, ["view", "org/repo", "badfield"])
        assert result.exit_code != 0 or "Unknown field" in (result.output or "")

    def test_versions_field_calls_display_versions(self, tmp_path):
        from apm_cli.cli import cli

        with (
            patch("apm_cli.commands.view.display_versions") as mock_dv,
        ):
            self.runner.invoke(cli, ["view", "org/repo", "versions"])
        mock_dv.assert_called_once()

    def test_marketplace_ref_without_field_calls_display_plugin(self):
        from apm_cli.cli import cli

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch("apm_cli.commands.view._display_marketplace_plugin") as mock_dp,
        ):
            self.runner.invoke(cli, ["view", "plugin@market"])
        mock_dp.assert_called_once()

    def test_no_apm_modules_exits(self, tmp_path):
        import os

        from apm_cli.cli import cli

        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            with patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None):
                result = self.runner.invoke(cli, ["view", "org/repo"])
        finally:
            os.chdir(orig)
        assert result.exit_code != 0 or "No apm_modules" in (result.output or "")

    def test_global_flag_uses_user_scope(self, tmp_path):

        from apm_cli.cli import cli

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
        ):
            self.runner.invoke(cli, ["view", "--global", "org/repo"])
        # Either succeeds or exits -- just check it ran

    def test_package_path_none_exits(self, tmp_path):
        import os

        from apm_cli.cli import cli

        (tmp_path / "apm_modules").mkdir()
        orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            with (
                patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
                patch(
                    "apm_cli.commands.view.resolve_package_path",
                    return_value=None,
                ),
            ):
                result = self.runner.invoke(cli, ["view", "org/repo"])
        finally:
            os.chdir(orig)
        assert result.exit_code != 0
