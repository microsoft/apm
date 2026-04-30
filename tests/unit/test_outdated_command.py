"""Unit tests for the ``apm outdated`` command."""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

# ---------------------------------------------------------------------------
# Patch targets -- imports are lazy (inside function body), so we patch
# at the source module level.
# ---------------------------------------------------------------------------
_PATCH_LOCKFILE = "apm_cli.deps.lockfile.LockFile"
_PATCH_GET_LOCKFILE_PATH = "apm_cli.deps.lockfile.get_lockfile_path"
_PATCH_MIGRATE = "apm_cli.deps.lockfile.migrate_lockfile_if_needed"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
_PATCH_AUTH = "apm_cli.core.auth.AuthResolver"
_PATCH_GET_APM_DIR = "apm_cli.core.scope.get_apm_dir"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _locked_dep(
    repo_url="org/pkg",
    host=None,
    resolved_ref="v1.0.0",
    resolved_commit="aaa",
    source=None,
    registry_prefix=None,
):
    """Build a LockedDependency with sensible defaults."""
    return LockedDependency(
        repo_url=repo_url,
        host=host,
        resolved_ref=resolved_ref,
        resolved_commit=resolved_commit,
        source=source,
        registry_prefix=registry_prefix,
    )


def _remote_tag(name, sha="abc123"):
    """Build a RemoteRef tag."""
    return RemoteRef(name=name, ref_type=GitReferenceType.TAG, commit_sha=sha)


def _remote_branch(name, sha="abc123"):
    """Build a RemoteRef branch."""
    return RemoteRef(name=name, ref_type=GitReferenceType.BRANCH, commit_sha=sha)


def _make_lockfile(deps_dict):
    """Create a LockFile with the given dependencies dict."""
    lf = LockFile()
    lf.dependencies = deps_dict
    return lf


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------
class TestOutdatedCommand:
    """Tests for ``apm outdated``."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    @contextlib.contextmanager
    def _chdir_tmp(self):
        """Create a temp dir, chdir into it, restore CWD on exit."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    # --- No lockfile ---

    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_no_lockfile_exits_1(self, mock_lf_cls, mock_get_apm_dir, mock_get_path, mock_migrate):
        """Exit 1 with error when no lockfile exists."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lf_cls.read.return_value = None

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 1
            assert "No lockfile" in result.output

    # --- Empty lockfile (no deps) ---

    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_empty_lockfile_success(
        self, mock_lf_cls, mock_get_apm_dir, mock_get_path, mock_migrate
    ):
        """Success message when lockfile has zero dependencies."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lf_cls.read.return_value = _make_lockfile({})

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "No locked dependencies" in result.output

    # --- All deps up-to-date ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_all_up_to_date(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Show success message when all deps are at latest tag."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/alpha": _locked_dep("org/alpha", resolved_ref="v2.0.0"),
                "org/beta": _locked_dep("org/beta", resolved_ref="v1.5.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            # Both repos: latest tag == locked tag
            mock_downloader.list_remote_refs.side_effect = [
                [_remote_tag("v2.0.0")],
                [_remote_tag("v1.5.0")],
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "up-to-date" in result.output.lower()

    # --- Some deps outdated ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_some_outdated_shows_table(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Table is shown when some deps are outdated; exit code is still 0."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/alpha": _locked_dep("org/alpha", resolved_ref="v1.0.0"),
                "org/beta": _locked_dep("org/beta", resolved_ref="v2.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.side_effect = [
                [_remote_tag("v2.0.0"), _remote_tag("v1.0.0")],  # alpha outdated
                [_remote_tag("v2.0.0")],  # beta up-to-date
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "org/alpha" in result.output
            assert "v1.0.0" in result.output
            assert "v2.0.0" in result.output
            assert "outdated" in result.output.lower()

    # --- Branch ref (SHA-based comparison) ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_branch_ref_outdated_when_sha_differs(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Branch-pinned dep is outdated when locked SHA differs from remote tip."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/branch-pkg": _locked_dep(
                    "org/branch-pkg", resolved_ref="main", resolved_commit="old_sha"
                ),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="new_sha_abc123"),
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "outdated" in result.output.lower()
            assert "org/branch-pkg" in result.output
            mock_downloader.list_remote_refs.assert_called_once()

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_branch_ref_up_to_date_when_sha_matches(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Branch-pinned dep is up-to-date when locked SHA matches remote tip."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/branch-pkg": _locked_dep(
                    "org/branch-pkg", resolved_ref="main", resolved_commit="same_sha"
                ),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="same_sha"),
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Should report all up-to-date (success message)
            assert "up-to-date" in result.output.lower() or "up to date" in result.output.lower()

    # --- Commit ref (unknown status — no matching branch) ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_commit_ref_shown_as_unknown(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Deps locked to a commit SHA show 'unknown' when ref is a raw SHA."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/commit-pkg": _locked_dep(
                    "org/commit-pkg",
                    resolved_ref="abc1234567890def1234567890abc1234567890de",
                ),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            # No branch matches the 40-char hex ref name
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="xyz999"),
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "unknown" in result.output.lower()
            assert "org/commit-pkg" in result.output

    # --- Local dep skipped ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_local_dep_skipped(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Local deps (source='local') should be skipped entirely."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "./local/pkg": _locked_dep("./local/pkg", resolved_ref="v1.0.0", source="local"),
                "org/remote": _locked_dep("org/remote", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [_remote_tag("v1.0.0")]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Local dep should not appear in output
            assert "./local/pkg" not in result.output
            # Only one call for the remote dep
            assert mock_downloader.list_remote_refs.call_count == 1

    # --- Artifactory dep skipped ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_artifactory_dep_skipped(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Artifactory deps (registry_prefix set) should be skipped."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "art/pkg": _locked_dep(
                    "art/pkg",
                    resolved_ref="v1.0.0",
                    registry_prefix="artifactory/github",
                ),
                "org/remote": _locked_dep("org/remote", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [_remote_tag("v1.0.0")]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "art/pkg" not in result.output
            assert mock_downloader.list_remote_refs.call_count == 1

    # --- Error fetching refs for one dep (graceful degradation) ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_error_fetching_refs_shows_unknown(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """When list_remote_refs raises for one dep, show 'unknown' and continue."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/fail-pkg": _locked_dep("org/fail-pkg", resolved_ref="v1.0.0"),
                "org/ok-pkg": _locked_dep("org/ok-pkg", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.side_effect = [
                RuntimeError("auth failed"),  # First dep errors
                [_remote_tag("v2.0.0"), _remote_tag("v1.0.0")],  # Second succeeds
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "org/fail-pkg" in result.output
            assert "unknown" in result.output.lower()
            # Second dep should still be processed
            assert "org/ok-pkg" in result.output
            assert mock_downloader.list_remote_refs.call_count == 2

    # --- No remote tags found ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_no_remote_tags_shows_unknown(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """When no remote tags are found, status should be 'unknown'."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/notags": _locked_dep("org/notags", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            # Only branches returned, no tags
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main"),
                _remote_branch("develop"),
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "unknown" in result.output.lower()

    # --- --global flag ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_global_flag_uses_user_scope(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """--global should resolve scope to USER (~/.apm/)."""
        with self._chdir_tmp() as tmp:
            user_apm = tmp / ".apm"
            user_apm.mkdir()
            mock_get_apm_dir.return_value = user_apm
            mock_get_path.return_value = user_apm / "apm.lock.yaml"
            mock_lf_cls.read.return_value = _make_lockfile({})

            result = self.runner.invoke(cli, ["outdated", "--global"])

            assert result.exit_code == 0
            # Verify get_apm_dir was called (scope is passed internally)
            mock_get_apm_dir.assert_called_once()

    # --- --verbose flag shows tags ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_verbose_shows_tags(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """--verbose should include available tags for outdated deps."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/pkg": _locked_dep("org/pkg", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v3.0.0"),
                _remote_tag("v2.0.0"),
                _remote_tag("v1.0.0"),
            ]

            result = self.runner.invoke(cli, ["outdated", "--verbose"])

            assert result.exit_code == 0
            # Should include tag listing in output
            assert "v3.0.0" in result.output
            assert "v2.0.0" in result.output

    # --- Mixed scenario: local + remote up-to-date + remote outdated + error ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_mixed_scenario(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Mix of local, up-to-date, outdated, and error deps."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "./my-local": _locked_dep("./my-local", source="local"),
                "org/current": _locked_dep("org/current", resolved_ref="v3.0.0"),
                "org/stale": _locked_dep("org/stale", resolved_ref="v1.0.0"),
                "org/broken": _locked_dep("org/broken", resolved_ref="v1.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.side_effect = [
                [_remote_tag("v3.0.0")],  # current: up-to-date
                [_remote_tag("v2.0.0"), _remote_tag("v1.0.0")],  # stale: outdated
                RuntimeError("network error"),  # broken: error
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Local dep not in output
            assert "./my-local" not in result.output
            # Current dep in output
            assert "org/current" in result.output
            # Stale dep in output with outdated status
            assert "org/stale" in result.output
            # Broken dep in output with unknown
            assert "org/broken" in result.output

    # --- Dep with no resolved_ref (default branch comparison) ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_no_resolved_ref_compares_against_default_branch(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Dep with no resolved_ref compares SHA against default branch tip."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/noref": _locked_dep("org/noref", resolved_ref=None, resolved_commit="old_sha"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="new_sha_def456"),
            ]

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "outdated" in result.output.lower()
            mock_downloader.list_remote_refs.assert_called_once()

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_no_resolved_ref_no_branches_shows_unknown(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Dep with no resolved_ref and no branches returns unknown."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/noref": _locked_dep("org/noref", resolved_ref=None, resolved_commit="old_sha"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_dl_cls.return_value = mock_downloader
            mock_downloader.list_remote_refs.return_value = []

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "unknown" in result.output.lower()

    # --- No lockfile with --global ---

    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_no_lockfile_global_exits_1(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
    ):
        """--global with no lockfile exits 1 with user-scope hint."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lf_cls.read.return_value = None

            result = self.runner.invoke(cli, ["outdated", "--global"])

            assert result.exit_code == 1
            assert "~/.apm/" in result.output

    # --- Virtual package deps ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_virtual_dep_processed_normally(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Virtual package deps are not skipped; their parent repo tags are fetched."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            virtual_dep = LockedDependency(
                repo_url="org/pkg",
                resolved_ref="v1.0.0",
                resolved_commit="abc",
                is_virtual=True,
                virtual_path="prompts/my.prompt.md",
            )
            # get_unique_key() for virtual deps returns "org/pkg/prompts/my.prompt.md"
            deps = {"org/pkg/prompts/my.prompt.md": virtual_dep}
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v1.0.0"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Remote refs were fetched -- virtual deps are NOT silently skipped
            mock_downloader.list_remote_refs.assert_called_once()

    # --- Dev dependency visibility ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_dev_dep_included_in_output(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Dev dependencies (is_dev=True) are included in the outdated check."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            dev_dep = LockedDependency(
                repo_url="org/devpkg",
                resolved_ref="v1.0.0",
                resolved_commit="abc",
                is_dev=True,
            )
            deps = {"org/devpkg": dev_dep}
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v2.0.0"),
                _remote_tag("v1.0.0"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Dev dep must appear in the output (is_dev does not suppress it)
            assert "org/devpkg" in result.output
            # And its status should be outdated since v2.0.0 > v1.0.0
            assert "outdated" in result.output.lower()

    # --- Multiple packages with same version ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_multiple_packages_same_version_all_shown(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Two packages pinned to the same version both appear in output (no dedup)."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/alpha": _locked_dep("org/alpha", resolved_ref="v2.0.0"),
                "org/beta": _locked_dep("org/beta", resolved_ref="v2.0.0"),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            # Both are already at the latest v2.0.0
            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v2.0.0"),
                _remote_tag("v1.0.0"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Both packages must be checked (list_remote_refs called twice)
            assert mock_downloader.list_remote_refs.call_count == 2

    # --- Parallel checks ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_parallel_checks_default(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """Default parallel-checks=4 should still check all deps."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                f"org/pkg{i}": _locked_dep(
                    f"org/pkg{i}",
                    resolved_ref=None,
                    resolved_commit="aaa",
                )
                for i in range(6)
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="aaa"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            assert "up-to-date" in result.output.lower()
            assert mock_downloader.list_remote_refs.call_count == 6

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_sequential_checks_flag(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """--parallel-checks 0 forces sequential checking."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/alpha": _locked_dep(
                    "org/alpha",
                    resolved_ref=None,
                    resolved_commit="aaa",
                ),
                "org/beta": _locked_dep(
                    "org/beta",
                    resolved_ref=None,
                    resolved_commit="aaa",
                ),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_branch("main", sha="aaa"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated", "--parallel-checks", "0"])

            assert result.exit_code == 0
            assert "up-to-date" in result.output.lower()
            assert mock_downloader.list_remote_refs.call_count == 2

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_parallel_checks_custom_value(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """--parallel-checks 2 uses at most 2 workers but checks all deps."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                f"org/pkg{i}": _locked_dep(
                    f"org/pkg{i}",
                    resolved_ref="v1.0.0",
                    resolved_commit="aaa",
                )
                for i in range(4)
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v2.0.0"),
                _remote_tag("v1.0.0"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated", "-j", "2"])

            assert result.exit_code == 0
            assert mock_downloader.list_remote_refs.call_count == 4
            assert "outdated" in result.output.lower()

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_parallel_check_exception_handled(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """A failing remote check in parallel mode should not crash."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            deps = {
                "org/good": _locked_dep(
                    "org/good",
                    resolved_ref="v1.0.0",
                    resolved_commit="aaa",
                ),
                "org/bad": _locked_dep(
                    "org/bad",
                    resolved_ref="v1.0.0",
                    resolved_commit="bbb",
                ),
            }
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()

            def _side_effect(dep_ref):
                if "bad" in (dep_ref.repo_url or ""):
                    raise ConnectionError("network down")
                return [_remote_tag("v1.0.0")]

            mock_downloader.list_remote_refs.side_effect = _side_effect
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated", "-j", "4"])

            # Should not crash -- bad dep becomes "unknown"
            assert result.exit_code == 0
            assert "unknown" in result.output.lower()

    # --- ADO dependency handling ---

    @patch(_PATCH_AUTH)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_GET_APM_DIR)
    @patch(_PATCH_LOCKFILE)
    def test_ado_dep_builds_correct_reference(
        self,
        mock_lf_cls,
        mock_get_apm_dir,
        mock_get_path,
        mock_migrate,
        mock_dl_cls,
        mock_auth,
    ):
        """ADO deps (host=dev.azure.com) should pass full URL to DependencyReference.parse()."""
        with self._chdir_tmp() as tmp:
            mock_get_apm_dir.return_value = tmp
            mock_get_path.return_value = tmp / "apm.lock.yaml"

            ado_dep = LockedDependency(
                repo_url="myorg/myproject/_git/myrepo",
                host="dev.azure.com",
                resolved_ref="v1.0.0",
                resolved_commit="aaa",
            )
            deps = {"myorg/myproject/_git/myrepo": ado_dep}
            mock_lf_cls.read.return_value = _make_lockfile(deps)

            mock_downloader = MagicMock()
            mock_downloader.list_remote_refs.return_value = [
                _remote_tag("v1.0.0"),
            ]
            mock_dl_cls.return_value = mock_downloader

            result = self.runner.invoke(cli, ["outdated"])

            assert result.exit_code == 0
            # Verify list_remote_refs was called (dep was not silently skipped)
            mock_downloader.list_remote_refs.assert_called_once()
