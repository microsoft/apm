"""Acceptance tests for APM CLI logging UX contract.

These tests verify the exact output contract for install command logging.
They use Click's CliRunner with mocked network calls — NO real tokens or
network access needed.

Each test validates output format, symbols, and message content against the
acceptance plan.
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
from apm_cli.utils.console import STATUS_SYMBOLS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InstallAcceptanceBase:
    """Shared fixtures for install logging acceptance tests."""

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
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    @staticmethod
    def _write_apm_yml(tmp: Path, deps=None, mcp_deps=None):
        """Write a minimal apm.yml."""
        data = {
            "name": "test-project",
            "dependencies": {
                "apm": deps or [],
                "mcp": mcp_deps or [],
            },
        }
        (tmp / "apm.yml").write_text(yaml.safe_dump(data, sort_keys=False))

    @staticmethod
    def _make_install_result(**kwargs):
        """Build an InstallResult with sensible defaults."""
        defaults = dict(
            installed_count=0,
            prompts_integrated=0,
            agents_integrated=0,
            diagnostics=MagicMock(
                has_diagnostics=False,
                has_critical_security=False,
                error_count=0,
            ),
        )
        defaults.update(kwargs)
        return InstallResult(**defaults)

    # Common patch targets
    _VALIDATE = "apm_cli.commands.install._validate_package_exists"
    _INSTALL_APM = "apm_cli.commands.install._install_apm_dependencies"
    _APM_PKG = "apm_cli.commands.install.APMPackage"
    _DEPS_AVAIL = "apm_cli.commands.install.APM_DEPS_AVAILABLE"
    _MIGRATE_LOCK = "apm_cli.commands.install.migrate_lockfile_if_needed"
    _LOCKFILE_READ = "apm_cli.commands.install.LockFile.read"
    _GET_LOCKPATH = "apm_cli.commands.install.get_lockfile_path"


# ---------------------------------------------------------------------------
# I1: Single public package, happy path
# ---------------------------------------------------------------------------


class TestI1SinglePublicPackageHappyPath(_InstallAcceptanceBase):
    """I1: Single public package installs successfully."""

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_happy_path_output(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg

        mock_install.return_value = self._make_install_result(installed_count=1)
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        out = result.output
        assert result.exit_code == 0, f"Exit {result.exit_code}: {out}"

        # Validation phase
        assert "Validating 1 package" in out
        assert "[+] owner/repo" in out

        # Installation phase
        assert "Installing" in out

        # Summary — 1 APM dependency
        assert "1 APM dependency" in out or "Installed 1 APM" in out


# ---------------------------------------------------------------------------
# I4: Package fails validation
# ---------------------------------------------------------------------------


class TestI4PackageFailsValidation(_InstallAcceptanceBase):
    """I4: Package fails validation — appropriate error output."""

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_not_accessible_message(self, mock_validate):
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/nonexistent"])

        out = result.output
        assert "not accessible or doesn't exist" in out
        assert "[x]" in out

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_verbose_hint_when_not_verbose(self, mock_validate):
        """Non-verbose mode shows --verbose hint."""
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/nonexistent"])

        assert "--verbose" in result.output

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_no_verbose_hint_when_verbose(self, mock_validate):
        """Verbose mode should NOT repeat the --verbose hint in the validation reason."""
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "--verbose", "owner/nonexistent"])

        # The validation failure reason should NOT contain the verbose hint
        # when already in verbose mode.
        lines_with_cross = [l for l in result.output.splitlines() if "[x]" in l]  # noqa: E741
        for line in lines_with_cross:
            assert "run with --verbose" not in line.lower(), (
                f"Redundant --verbose hint found in verbose mode: {line}"
            )

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_all_failed_summary(self, mock_validate):
        """When all packages fail, summary says 'Nothing to install'."""
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/nonexistent"])

        assert (
            "All packages failed validation" in result.output
            or "Nothing to install" in result.output
        )


# ---------------------------------------------------------------------------
# I5: Package already installed
# ---------------------------------------------------------------------------


class TestI5PackageAlreadyInstalled(_InstallAcceptanceBase):
    """I5: Package already in apm.yml."""

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_already_installed_message(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg
        mock_install.return_value = self._make_install_result(installed_count=1)
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            # Pre-populate apm.yml WITH the package already listed
            self._write_apm_yml(tmp, deps=["owner/repo"])
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        out = result.output
        assert "already in apm.yml" in out


# ---------------------------------------------------------------------------
# I6: Mixed valid + invalid packages
# ---------------------------------------------------------------------------


class TestI6MixedValidInvalid(_InstallAcceptanceBase):
    """I6: First package validates, second doesn't."""

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_mixed_shows_check_and_cross(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        # First package valid, second invalid
        mock_validate.side_effect = [True, False]

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="good/pkg", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg
        mock_install.return_value = self._make_install_result(installed_count=1)
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "good/pkg", "bad/missing"])

        out = result.output
        assert result.exit_code == 0, f"Exit {result.exit_code}: {out}"

        # Check mark for good package, cross for bad
        assert "[+]" in out, "Expected [+] for valid package"
        assert "[x]" in out, "Expected [x] for invalid package"

        # Continues to install the valid one
        assert "1" in out and "failed validation" in out


# ---------------------------------------------------------------------------
# I7: Full manifest install, up to date
# ---------------------------------------------------------------------------


class TestI7ManifestUpToDate(_InstallAcceptanceBase):
    """I7: No packages arg, deps up to date."""

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    def test_up_to_date_or_no_deps(
        self,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = []
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg
        mock_install.return_value = self._make_install_result()
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp, deps=["owner/cached-pkg"])
            result = self.runner.invoke(cli, ["install"])

        out = result.output
        # Should indicate nothing new was done, or summary with 0
        assert result.exit_code == 0, f"Exit {result.exit_code}: {out}"


# ---------------------------------------------------------------------------
# Logging rules: Traffic-light, non-verbose, verbose, dry-run, symbols
# ---------------------------------------------------------------------------


class TestLoggingRules(_InstallAcceptanceBase):
    """Verify logging traffic-light rules and verbosity contracts."""

    # --- Non-verbose contract ---

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_non_verbose_no_auth_details(self, mock_validate):
        """Non-verbose output must NOT contain auth debug details."""
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        out = result.output
        assert "Auth resolved" not in out
        assert "API" not in out
        assert "git ls-remote" not in out

    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_non_verbose_has_verbose_hint(self, mock_validate):
        """Non-verbose failure should suggest --verbose."""
        mock_validate.return_value = False

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        assert "--verbose" in result.output

    # --- Dry-run contract ---

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_dry_run_shows_dry_run_label(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        """--dry-run output must say 'dry run' or 'Dry run'."""
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "--dry-run", "owner/repo"])

        out = result.output.lower()
        assert "dry run" in out or "dry-run" in out, (
            f"Expected dry-run label in output:\n{result.output}"
        )

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_dry_run_no_file_changes(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        """--dry-run must not write to apm.yml beyond the initial package addition."""
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            original = (tmp / "apm.yml").read_text()

            result = self.runner.invoke(cli, ["install", "--dry-run", "owner/repo"])  # noqa: F841

            # apm.yml should be unchanged (dry-run skips writing)
            final = (tmp / "apm.yml").read_text()
            assert original == final, "Dry-run modified apm.yml"

    # --- Symbol consistency ---

    def test_status_symbols_are_ascii_brackets(self):
        """All STATUS_SYMBOLS must be ASCII bracket format [x]."""
        bracket_pattern = {"[*]", "[>]", "[i]", "[!]", "[x]", "[+]", "[#]"}
        for key, sym in STATUS_SYMBOLS.items():
            assert sym in bracket_pattern, (
                f"STATUS_SYMBOLS['{key}'] = '{sym}' is not a valid bracket symbol"
            )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths(_InstallAcceptanceBase):
    """Verify error output patterns and --verbose hints."""

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_install_error_verbose_hint(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        """When _install_apm_dependencies raises, non-verbose shows hint."""
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg

        mock_install.side_effect = RuntimeError("download timed out")
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        out = result.output
        assert result.exit_code == 1
        assert "Run with --verbose" in out

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_install_error_no_hint_when_verbose(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        """When --verbose is active, don't show the --verbose hint."""
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg

        mock_install.side_effect = RuntimeError("download timed out")
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "--verbose", "owner/repo"])

        out = result.output
        assert result.exit_code == 1
        assert "Run with --verbose" not in out

    @patch(_InstallAcceptanceBase._GET_LOCKPATH)
    @patch(_InstallAcceptanceBase._LOCKFILE_READ)
    @patch(_InstallAcceptanceBase._MIGRATE_LOCK)
    @patch(_InstallAcceptanceBase._INSTALL_APM)
    @patch(_InstallAcceptanceBase._APM_PKG)
    @patch(_InstallAcceptanceBase._DEPS_AVAIL, True)
    @patch(_InstallAcceptanceBase._VALIDATE)
    def test_diagnostics_render_before_summary(
        self,
        mock_validate,
        mock_apm_pkg,
        mock_install,
        mock_migrate,
        mock_lock_read,
        mock_lock_path,
    ):
        """Diagnostics section must appear before final install summary."""
        mock_validate.return_value = True

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", reference="main")]
        pkg.get_mcp_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        mock_apm_pkg.from_apm_yml.return_value = pkg

        # Build a real DiagnosticCollector with some content
        from apm_cli.utils.diagnostics import DiagnosticCollector

        diag = DiagnosticCollector()
        diag.warn("test-pkg", "some warning")

        mock_install.return_value = self._make_install_result(
            installed_count=1,
            diagnostics=diag,
        )
        mock_lock_read.return_value = None
        mock_lock_path.return_value = Path("apm.lock.yaml")

        with self._chdir_tmp() as tmp:
            self._write_apm_yml(tmp)
            result = self.runner.invoke(cli, ["install", "owner/repo"])

        out = result.output
        assert result.exit_code == 0, f"Exit {result.exit_code}: {out}"

        # Diagnostics separator appears before summary
        diag_pos = out.find("Diagnostics")
        summary_pos = out.find("Installed")
        if diag_pos != -1 and summary_pos != -1:
            assert diag_pos < summary_pos, "Diagnostics should render BEFORE the install summary"
