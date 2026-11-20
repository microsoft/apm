"""tests for apm_cli.commands.outdated (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(
    *,
    key="org/repo",
    host="github.com",
    repo_url=None,
    resolved_ref="main",
    resolved_commit="abc123456789",
    source="github",
    registry_prefix=None,
    discovered_via=None,
    marketplace_plugin_name=None,
):
    return LockedDependency(
        repo_url=repo_url or key,
        host=host,
        resolved_ref=resolved_ref,
        resolved_commit=resolved_commit,
        source=source,
        registry_prefix=registry_prefix,
        discovered_via=discovered_via,
        marketplace_plugin_name=marketplace_plugin_name,
    )


@pytest.fixture(params=["locked", "mock"])
def make_dep(request):
    """Build deps as real LockedDependency objects or as MagicMock stand-ins."""
    return _make_dep if request.param == "locked" else _make_mock_dep


# ---------------------------------------------------------------------------
# _find_remote_tip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_marketplace_ref
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_one_dep
# ---------------------------------------------------------------------------


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

    def test_filters_local_and_registry_deps(self, make_dep):
        mock_logger = MagicMock(verbose=False)
        local_dep = make_dep(key="local/dep", source="local")
        reg_dep = make_dep(key="reg/dep", registry_prefix="artifactory")
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

    def test_all_up_to_date_logs_success(self, make_dep):
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = make_dep()
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


# ---------------------------------------------------------------------------
# _check_parallel_plain
# ---------------------------------------------------------------------------


def _make_mock_dep(
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
    dep.source_url = None
    dep.is_virtual = False
    dep.virtual_path = None
    dep.replacement = None
    return dep


class TestOutdatedCommandCoverage:
    """Additional tests for outdated CLI command missed lines."""

    def setup_method(self):
        self.runner = CliRunner()

    def _run(self, *args, env=None):
        from apm_cli.cli import cli

        return self.runner.invoke(cli, ["outdated", *args], env=env)

    def test_apm_no_cache_skips_git_cache(self):
        """Line 342→348: APM_NO_CACHE set → skip git cache setup."""
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = _make_mock_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}
        uptodate_row = OutdatedRow(
            package="org/repo", current="abc", latest="abc", status="up-to-date"
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
            result = self._run(env={"APM_NO_CACHE": "1"})
        assert result.exit_code == 0

    def test_git_cache_oserror_skipped(self):
        """Lines 346-347: OSError from GitCache → silently skipped."""
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = _make_mock_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}
        uptodate_row = OutdatedRow(
            package="org/repo", current="abc", latest="abc", status="up-to-date"
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
            patch("apm_cli.cache.git_cache.GitCache", side_effect=OSError("no cache dir")),
            patch(
                "apm_cli.commands.outdated._check_deps_with_progress",
                return_value=[uptodate_row],
            ),
        ):
            result = self._run()
        assert result.exit_code == 0

    def test_check_deps_returns_empty_rows(self):
        """Lines 383-384: _check_deps_with_progress returns [] → 'No remote deps'."""

        mock_logger = MagicMock(verbose=False)
        dep = _make_mock_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}

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
            patch("apm_cli.commands.outdated._check_deps_with_progress", return_value=[]),
        ):
            result = self._run()
        assert result.exit_code == 0

    def test_plain_text_fallback_when_no_console(self):
        """Lines 402, 437-446: console=None → ImportError → plain text table."""
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = _make_mock_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}
        outdated_row = OutdatedRow(
            package="org/repo", current="abc", latest="def", status="outdated"
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
                return_value=[outdated_row],
            ),
            patch("apm_cli.commands._helpers._get_console", return_value=None),
        ):
            result = self._run()
        assert result.exit_code == 0  # outdated detected but no sys.exit(1)

    def test_unknown_rows_logs_some_not_checked(self):
        """Lines 455→exit: has_unknown=True, has_outdated=False → progress message."""
        from apm_cli.commands.outdated import OutdatedRow

        mock_logger = MagicMock(verbose=False)
        dep = _make_mock_dep()
        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"org/repo": dep}
        unknown_row = OutdatedRow(package="org/repo", current="abc", latest="-", status="unknown")

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
                return_value=[unknown_row],
            ),
            patch("apm_cli.commands._helpers._get_console", return_value=None),
        ):
            result = self._run()
        assert result.exit_code == 0
