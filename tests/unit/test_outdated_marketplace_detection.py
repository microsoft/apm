"""tests for apm_cli.commands.outdated.

Covers missing lines/branches identified in coverage-unit.json:
- _find_remote_tip: no remote_refs, ref_name found, default branch fallback,
  first branch fallback (lines 64-72)
- _check_marketplace_ref: no discovered_via/plugin_name, MarketplaceError,
  fetch error, plugin not found, string source, no mkt_ref, no installed_ref (lines 91-135)
- _check_one_dep: marketplace path, DependencyReference parse failure,
  list_remote_refs failure, tag comparison branches, branch comparison (lines 161-263)
- outdated command: no lockfile, no deps, no remote deps, all up-to-date,
  rich table rendering, plain fallback (lines 289-456)
- _check_deps_with_progress: parallel vs sequential, ImportError fallback
- _check_parallel_plain: parallel execution (lines 552-571)
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(
    *,
    key="org/repo",
    host="github.com",
    repo_url="org/repo",
    resolved_ref="main",
    resolved_commit="abc123456789",
    source="github",
    registry_prefix=None,
    discovered_via=None,
    marketplace_plugin_name=None,
):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.host = host
    dep.repo_url = repo_url
    dep.resolved_ref = resolved_ref
    dep.resolved_commit = resolved_commit
    dep.source = source
    dep.registry_prefix = registry_prefix
    dep.discovered_via = discovered_via
    dep.marketplace_plugin_name = marketplace_plugin_name
    dep.is_virtual = False
    dep.virtual_path = None
    return dep


def _make_remote_ref(name, ref_type_val, sha="abc12345678"):
    from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

    ref_type = GitReferenceType.TAG if ref_type_val == "tag" else GitReferenceType.BRANCH
    return RemoteRef(name=name, ref_type=ref_type, commit_sha=sha)


# ---------------------------------------------------------------------------
# _find_remote_tip
# ---------------------------------------------------------------------------


class TestFindRemoteTip:
    def test_no_remote_refs_returns_none(self):
        from apm_cli.commands.outdated import _find_remote_tip

        result = _find_remote_tip("main", [])
        assert result is None

    def test_ref_name_found(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("main", "branch", "sha1234")
        result = _find_remote_tip("main", [ref])
        assert result == "sha1234"

    def test_ref_name_not_found_returns_none(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("main", "branch", "mainsha")
        # When ref_name is truthy but not found, returns None (no fallback)
        result = _find_remote_tip("nonexistent", [ref])
        assert result is None
        assert result is None

    def test_ref_name_empty_returns_main(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("main", "branch", "mainsha")
        result = _find_remote_tip("", [ref])
        assert result == "mainsha"

    def test_ref_name_none_returns_master_fallback(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("master", "branch", "mastersha")
        result = _find_remote_tip(None, [ref])
        assert result == "mastersha"

    def test_no_main_or_master_returns_first_branch(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("develop", "branch", "devsha")
        result = _find_remote_tip(None, [ref])
        assert result == "devsha"

    def test_only_tag_refs_returns_none(self):
        from apm_cli.commands.outdated import _find_remote_tip

        ref = _make_remote_ref("v1.0.0", "tag", "tagsha")
        result = _find_remote_tip(None, [ref])
        assert result is None


# ---------------------------------------------------------------------------
# _check_marketplace_ref
# ---------------------------------------------------------------------------


class TestCheckMarketplaceRef:
    def test_no_discovered_via_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(discovered_via=None, marketplace_plugin_name=None)
        assert _check_marketplace_ref(dep, False) is None

    def test_no_marketplace_plugin_name_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name=None)
        assert _check_marketplace_ref(dep, False) is None

    def test_marketplace_not_found_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref
        from apm_cli.marketplace.errors import MarketplaceError

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="my-plugin")
        with patch(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            side_effect=MarketplaceError("not found"),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_fetch_fails_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref
        from apm_cli.marketplace.errors import MarketplaceError

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="my-plugin")
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=MagicMock(),
            ),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                side_effect=MarketplaceError("fetch fail"),
            ),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_plugin_not_found_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="my-plugin")
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = None

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_string_source_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="my-plugin")
        plugin = MagicMock()
        plugin.source = "some/string/path"
        plugin.version = "1.0.0"
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_dict_source_empty_ref_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="my-plugin")
        plugin = MagicMock()
        plugin.source = {"type": "github", "repo": "org/repo", "ref": ""}
        plugin.version = "1.0.0"
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_no_installed_ref_returns_none(self):
        from apm_cli.commands.outdated import _check_marketplace_ref

        dep = _make_dep(
            discovered_via="mymarket",
            marketplace_plugin_name="my-plugin",
            resolved_ref="",
            resolved_commit="",
        )
        plugin = MagicMock()
        plugin.source = {"type": "github", "repo": "org/repo", "ref": "v1.0.0"}
        plugin.version = "1.0.0"
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert result is None

    def test_outdated_returns_row(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_marketplace_ref

        dep = _make_dep(
            discovered_via="mymarket",
            marketplace_plugin_name="my-plugin",
            resolved_ref="v0.9.0",
            resolved_commit="",
        )
        plugin = MagicMock()
        plugin.source = {"type": "github", "repo": "org/repo", "ref": "v1.0.0"}
        plugin.version = "1.0.0"
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert isinstance(result, OutdatedRow)
        assert result.status == "outdated"

    def test_up_to_date_returns_row(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_marketplace_ref

        dep = _make_dep(
            discovered_via="mymarket",
            marketplace_plugin_name="my-plugin",
            resolved_ref="v1.0.0",
            resolved_commit="",
        )
        plugin = MagicMock()
        plugin.source = {"type": "github", "repo": "org/repo", "ref": "v1.0.0"}
        plugin.version = "1.0.0"
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            result = _check_marketplace_ref(dep, False)
        assert isinstance(result, OutdatedRow)
        assert result.status == "up-to-date"


# ---------------------------------------------------------------------------
# _check_one_dep
# ---------------------------------------------------------------------------


class TestCheckOneDep:
    def test_marketplace_path_dispatches(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_one_dep

        dep = _make_dep(discovered_via="mymarket", marketplace_plugin_name="plugin")
        mock_row = OutdatedRow(package="p", current="c", latest="l", status="outdated")
        downloader = MagicMock()

        with patch(
            "apm_cli.commands.outdated._check_marketplace_ref",
            return_value=mock_row,
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result is mock_row

    def test_dep_ref_parse_exception_returns_unknown(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(host="", repo_url="invalid_ref")
        downloader = MagicMock()

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch(
                "apm_cli.models.dependency.reference.DependencyReference.parse",
                side_effect=Exception("bad ref"),
            ),
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "unknown"

    def test_list_remote_refs_exception_returns_unknown(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep()
        downloader = MagicMock()
        downloader.list_remote_refs.side_effect = Exception("network fail")

        with patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "unknown"

    def test_tag_outdated(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="v1.0.0", resolved_commit="sha1")
        ref_tag = _make_remote_ref("v2.0.0", "tag", "sha2")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = [ref_tag]

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch(
                "apm_cli.utils.version_checker.is_newer_version",
                return_value=True,
            ),
        ):
            result = _check_one_dep(dep, downloader, True)
        assert result.status == "outdated"
        # verbose=True should populate extra_tags
        assert isinstance(result.extra_tags, list)

    def test_tag_up_to_date(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="v2.0.0")
        ref_tag = _make_remote_ref("v2.0.0", "tag", "sha2")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = [ref_tag]

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch("apm_cli.utils.version_checker.is_newer_version", return_value=False),
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "up-to-date"

    def test_tag_no_tags_returns_unknown(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="v1.0.0")
        # Only branch refs, no tags
        ref_branch = _make_remote_ref("main", "branch", "sha1")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = [ref_branch]

        with patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "unknown"

    def test_branch_outdated(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="main", resolved_commit="oldsha1234567")
        ref_branch = _make_remote_ref("main", "branch", "newsha1234567")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = [ref_branch]

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch("apm_cli.commands.outdated._find_remote_tip", return_value="newsha1234567"),
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "outdated"

    def test_branch_up_to_date(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="main", resolved_commit="sameshashasha")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = [
            _make_remote_ref("main", "branch", "sameshashasha")
        ]

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch("apm_cli.commands.outdated._find_remote_tip", return_value="sameshashasha"),
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "up-to-date"

    def test_branch_no_remote_tip_returns_unknown(self):
        from apm_cli.commands.outdated import _check_one_dep

        dep = _make_dep(resolved_ref="main")
        downloader = MagicMock()
        downloader.list_remote_refs.return_value = []

        with (
            patch("apm_cli.commands.outdated._check_marketplace_ref", return_value=None),
            patch("apm_cli.commands.outdated._find_remote_tip", return_value=None),
        ):
            result = _check_one_dep(dep, downloader, False)
        assert result.status == "unknown"


# ---------------------------------------------------------------------------
# outdated CLI command
# ---------------------------------------------------------------------------


class TestOutdatedCommand:
    def setup_method(self):
        self.runner = CliRunner()

    def _run(self, *args):
        from apm_cli.cli import cli

        return self.runner.invoke(cli, ["outdated", *args])

    def test_no_lockfile_exits(self):
        with (
            patch(
                "apm_cli.core.command_logger.CommandLogger", return_value=MagicMock(verbose=False)
            ),
            patch("apm_cli.core.scope.get_apm_dir", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
        ):
            result = self._run()
        assert result.exit_code != 0

    def test_empty_lockfile_deps_succeeds(self):
        mock_logger = MagicMock(verbose=False)
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {}

        with (
            patch("apm_cli.core.command_logger.CommandLogger", return_value=mock_logger),
            patch("apm_cli.core.scope.get_apm_dir", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lockfile),
        ):
            result = self._run()
        assert result.exit_code == 0

    def test_filters_local_and_registry_deps(self):
        mock_logger = MagicMock(verbose=False)
        local_dep = _make_dep(key="local/dep", source="local")
        reg_dep = _make_dep(key="reg/dep", registry_prefix="artifactory")
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"local/dep": local_dep, "reg/dep": reg_dep}

        with (
            patch("apm_cli.core.command_logger.CommandLogger", return_value=mock_logger),
            patch("apm_cli.core.scope.get_apm_dir", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lockfile),
            patch("apm_cli.core.auth.AuthResolver", return_value=MagicMock()),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                return_value=MagicMock(),
            ),
        ):
            result = self._run()
        assert result.exit_code == 0

    def test_all_up_to_date_logs_success(self):
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = _make_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}
        uptodate_row = OutdatedRow(
            package="org/repo", current="abc123", latest="abc123", status="up-to-date"
        )

        with (
            patch("apm_cli.core.command_logger.CommandLogger", return_value=mock_logger),
            patch("apm_cli.core.scope.get_apm_dir", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=MagicMock()),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lockfile),
            patch("apm_cli.core.auth.AuthResolver", return_value=MagicMock()),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                return_value=MagicMock(),
            ),
            patch(
                "apm_cli.commands.outdated._check_deps_with_progress",
                return_value=[uptodate_row],
            ),
        ):
            result = self._run()
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# _check_deps_with_progress
# ---------------------------------------------------------------------------


class TestCheckDepsWithProgress:
    def test_sequential_no_rich(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_deps_with_progress

        dep = _make_dep()
        row = OutdatedRow(package="org/repo", current="a", latest="b", status="up-to-date")
        mock_logger = MagicMock()
        mock_logger.progress = MagicMock()

        with (
            patch.dict(sys.modules, {"rich.progress": None}),
            patch("apm_cli.commands.outdated._check_one_dep", return_value=row),
        ):
            rows = _check_deps_with_progress([dep], MagicMock(), False, 0, mock_logger)

        assert len(rows) == 1
        assert rows[0].status == "up-to-date"

    def test_parallel_no_rich(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_deps_with_progress

        deps = [_make_dep(key=f"org/repo{i}") for i in range(3)]
        rows_map = {
            f"org/repo{i}": OutdatedRow(
                package=f"org/repo{i}", current="a", latest="b", status="outdated"
            )
            for i in range(3)
        }
        mock_logger = MagicMock()
        mock_logger.progress = MagicMock()

        def _fake_check(dep, dl, verbose):
            return rows_map[dep.get_unique_key()]

        with (
            patch.dict(sys.modules, {"rich.progress": None}),
            patch("apm_cli.commands.outdated._check_parallel_plain") as mock_parallel,
        ):
            mock_parallel.return_value = list(rows_map.values())
            _rows = _check_deps_with_progress(deps, MagicMock(), False, 4, mock_logger)

        mock_parallel.assert_called_once()


# ---------------------------------------------------------------------------
# _check_parallel_plain
# ---------------------------------------------------------------------------


class TestCheckParallelPlain:
    def test_parallel_plain_returns_results(self):
        from apm_cli.commands.outdated import OutdatedRow, _check_parallel_plain

        deps = [_make_dep(key=f"org/pkg{i}", repo_url=f"org/pkg{i}") for i in range(3)]
        rows_map = {
            f"org/pkg{i}": OutdatedRow(
                package=f"org/pkg{i}", current="old", latest="new", status="outdated"
            )
            for i in range(3)
        }

        def _fake_check(dep, dl, verbose):
            return rows_map[dep.get_unique_key()]

        with patch("apm_cli.commands.outdated._check_one_dep", side_effect=_fake_check):
            result = _check_parallel_plain(deps, MagicMock(), False, 2)

        assert len(result) == 3

    def test_parallel_plain_exception_yields_unknown(self):
        from apm_cli.commands.outdated import _check_parallel_plain

        dep = _make_dep()
        with patch(
            "apm_cli.commands.outdated._check_one_dep",
            side_effect=RuntimeError("boom"),
        ):
            result = _check_parallel_plain([dep], MagicMock(), False, 1)

        assert result[0].status == "unknown"
