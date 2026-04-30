"""Tests for `apm deps update` command.

Validates CLI wiring, flag propagation to the install engine, error handling,
and update-specific output (SHA diffs). The install engine itself is mocked --
these are CLI-layer tests, not integration tests.
"""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.results import InstallResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_apm_yml(deps=None, dev_deps=None):
    """Return a minimal apm.yml dict with the given APM deps."""
    data = {"name": "test-project", "version": "1.0.0"}
    if deps:
        data["dependencies"] = {d: "main" for d in deps}
    if dev_deps:
        data["devDependencies"] = {d: "main" for d in dev_deps}
    return yaml.dump(data, default_flow_style=False)


def _mock_dep(repo_url, alias=None):
    """Create a mock DependencyReference with required methods."""
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.alias = alias
    dep.get_unique_key.return_value = repo_url
    dep.get_display_name.return_value = alias or repo_url
    dep.reference = "main"
    dep.is_local = False
    dep.local_path = None
    dep.is_virtual = False
    dep.virtual_path = None
    return dep


def _mock_locked_dep(repo_url, sha, ref="main"):
    """Create a mock LockedDependency."""
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.resolved_commit = sha
    dep.resolved_ref = ref
    dep.get_unique_key.return_value = repo_url
    return dep


# Common patch targets -- the update() function uses lazy imports.
_PATCH_ENGINE = "apm_cli.commands.install._install_apm_dependencies"
_PATCH_DEPS_AVAILABLE = "apm_cli.commands.install.APM_DEPS_AVAILABLE"
_PATCH_APM_PACKAGE = "apm_cli.commands.deps.cli.APMPackage"
_PATCH_LOCKFILE = "apm_cli.deps.lockfile.LockFile"
_PATCH_GET_LOCKFILE_PATH = "apm_cli.deps.lockfile.get_lockfile_path"
_PATCH_MIGRATE = "apm_cli.deps.lockfile.migrate_lockfile_if_needed"
_PATCH_AUTH = "apm_cli.core.auth.AuthResolver"


class TestDepsUpdateCommand:
    """Test `apm deps update` CLI wiring and output."""

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

    # ------------------------------------------------------------------
    # Pre-flight validation
    # ------------------------------------------------------------------

    def test_no_apm_yml_exits_1(self):
        """Exit 1 when no apm.yml exists."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 1
            assert "No apm.yml found" in result.output

    @patch(_PATCH_APM_PACKAGE)
    def test_no_deps_exits_0(self, mock_pkg_cls):
        """Exit 0 with informational message when apm.yml has no APM deps."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml())
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = []
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 0
            assert "No APM dependencies" in result.output

    @patch(_PATCH_APM_PACKAGE)
    def test_unknown_package_exits_1(self, mock_pkg_cls):
        """Exit 1 when requested package isn't in apm.yml."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/real-pkg"]))
            mock_pkg = MagicMock()
            dep = _mock_dep("org/real-pkg")
            mock_pkg.get_apm_dependencies.return_value = [dep]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            result = self.runner.invoke(cli, ["deps", "update", "org/nonexistent"])
            assert result.exit_code == 1
            assert "not found in apm.yml" in result.output

    @patch(_PATCH_APM_PACKAGE)
    def test_unknown_package_shows_available(self, mock_pkg_cls):
        """Error message lists available packages."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/real-pkg"]))
            mock_pkg = MagicMock()
            dep = _mock_dep("org/real-pkg")
            mock_pkg.get_apm_dependencies.return_value = [dep]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            result = self.runner.invoke(cli, ["deps", "update", "org/nonexistent"])
            assert "Available:" in result.output
            assert "org/real-pkg" in result.output

    # ------------------------------------------------------------------
    # Engine delegation
    # ------------------------------------------------------------------

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_update_all_passes_update_refs_true(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """update_refs=True passed when no packages specified."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 0
            mock_engine.assert_called_once()
            _, kwargs = mock_engine.call_args
            assert kwargs["update_refs"] is True
            assert kwargs["only_packages"] is None

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_update_single_passes_only_packages(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """only_packages=['org/pkg'] passed when package arg given."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "org/pkg"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["only_packages"] == ["org/pkg"]
            assert kwargs["update_refs"] is True

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_short_name_normalized_to_canonical_key(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """Short repo basename is normalized to canonical owner/repo key."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["owner/compliance-rules"]))
            mock_pkg = MagicMock()
            dep = _mock_dep("owner/compliance-rules")
            mock_pkg.get_apm_dependencies.return_value = [dep]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "compliance-rules"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["only_packages"] == ["owner/compliance-rules"]
            assert kwargs["update_refs"] is True

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_force_flag_propagates(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """--force propagates to engine."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "--force"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["force"] is True

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_target_flag_propagates(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """--target propagates to engine."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "--target", "claude"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["target"] == "claude"

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_logger_passed_to_engine(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """An InstallLogger is passed to the engine for verbose output."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["logger"] is not None
            logger = kwargs["logger"]
            assert hasattr(logger, "download_complete")
            assert hasattr(logger, "lockfile_entry")

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_verbose_flag_propagates(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """--verbose propagates to engine and to the logger."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "--verbose"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["verbose"] is True
            assert kwargs["logger"].verbose is True

    # ------------------------------------------------------------------
    # Output tests
    # ------------------------------------------------------------------

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_sha_diff_shown_when_changed(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """Output contains 'old_sha -> new_sha' when packages change."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"

            # Old lockfile: SHA aaa...
            old_locked = _mock_locked_dep("org/pkg", "aaa11111222233334444555566667777")
            old_lockfile = MagicMock()
            old_lockfile.dependencies = {"org/pkg": old_locked}

            # New lockfile: SHA bbb...
            new_locked = _mock_locked_dep("org/pkg", "bbb11111222233334444555566667777")
            new_lockfile = MagicMock()
            new_lockfile.dependencies = {"org/pkg": new_locked}

            # First read returns old, second read (after engine) returns new
            mock_lockfile_cls.read.side_effect = [old_lockfile, new_lockfile]
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 0
            assert "aaa11111" in result.output
            assert "bbb11111" in result.output
            assert "->" in result.output
            assert "Updated 1 package" in result.output

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_already_latest_message(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """Shows 'already at latest refs' when SHAs unchanged."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"

            # Same SHA before and after
            same_sha = "aaa11111222233334444555566667777"
            locked = _mock_locked_dep("org/pkg", same_sha)
            lockfile = MagicMock()
            lockfile.dependencies = {"org/pkg": locked}
            mock_lockfile_cls.read.return_value = lockfile
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 0
            assert "already at latest refs" in result.output

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_engine_failure_exits_1(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """sys.exit(1) when engine raises an exception."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [_mock_dep("org/pkg")]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.side_effect = RuntimeError("Network timeout")

            result = self.runner.invoke(cli, ["deps", "update"])
            assert result.exit_code == 1
            assert "Update failed" in result.output

    # ------------------------------------------------------------------
    # Multi-package support
    # ------------------------------------------------------------------

    @patch(_PATCH_AUTH)
    @patch(_PATCH_MIGRATE)
    @patch(_PATCH_GET_LOCKFILE_PATH)
    @patch(_PATCH_LOCKFILE)
    @patch(_PATCH_ENGINE)
    @patch(_PATCH_APM_PACKAGE)
    def test_multiple_packages_propagate(
        self,
        mock_pkg_cls,
        mock_engine,
        mock_lockfile_cls,
        mock_get_path,
        mock_migrate,
        mock_auth,
    ):
        """Multiple package args propagate as only_packages list."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_minimal_apm_yml(deps=["org/pkg-a", "org/pkg-b"]))
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [
                _mock_dep("org/pkg-a"),
                _mock_dep("org/pkg-b"),
            ]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg_cls.from_apm_yml.return_value = mock_pkg

            mock_get_path.return_value = tmp / "apm.lock.yaml"
            mock_lockfile_cls.read.return_value = None
            mock_engine.return_value = InstallResult()

            result = self.runner.invoke(cli, ["deps", "update", "org/pkg-a", "org/pkg-b"])
            assert result.exit_code == 0
            _, kwargs = mock_engine.call_args
            assert kwargs["only_packages"] == ["org/pkg-a", "org/pkg-b"]


class TestDeadCodeRemoval:
    """Verify broken update helpers have been removed."""

    def test_update_single_package_removed(self):
        """_update_single_package deleted from _utils.py."""
        from apm_cli.commands.deps import _utils as utils

        assert not hasattr(utils, "_update_single_package")

    def test_update_all_packages_removed(self):
        """_update_all_packages deleted from _utils.py."""
        from apm_cli.commands.deps import _utils as utils

        assert not hasattr(utils, "_update_all_packages")

    def test_not_in_package_exports(self):
        """Dead functions not exported from __init__.py."""
        import apm_cli.commands.deps as deps_mod

        all_names = getattr(deps_mod, "__all__", [])
        assert "_update_single_package" not in all_names
        assert "_update_all_packages" not in all_names
