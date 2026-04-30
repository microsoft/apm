"""Tests for apm deps clean command --dry-run and --yes flags."""

import contextlib
import os
import tempfile
from pathlib import Path

import pytest  # noqa: F401
from click.testing import CliRunner

from apm_cli.cli import cli


class TestDepsCleanCommand:
    """Tests for apm deps clean --dry-run and --yes flags."""

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

    def _create_fake_apm_modules(self, root: Path) -> Path:
        """Create a fake apm_modules/ with one installed package."""
        pkg_dir = root / "apm_modules" / "testorg" / "testrepo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: testrepo\n")
        return root / "apm_modules"

    def test_dry_run_leaves_apm_modules_intact(self):
        """--dry-run must not remove apm_modules/."""
        with self._chdir_tmp() as tmp:
            apm_modules = self._create_fake_apm_modules(tmp)

            result = self.runner.invoke(cli, ["deps", "clean", "--dry-run"])

            assert result.exit_code == 0
            assert apm_modules.exists(), "apm_modules/ must not be removed in dry-run mode"
            assert "Dry run" in result.output

    def test_dry_run_lists_packages(self):
        """--dry-run should show the packages that would be removed."""
        with self._chdir_tmp() as tmp:
            self._create_fake_apm_modules(tmp)

            result = self.runner.invoke(cli, ["deps", "clean", "--dry-run"])

            assert result.exit_code == 0
            assert "testorg/testrepo" in result.output

    def test_yes_flag_skips_confirmation(self):
        """--yes must remove apm_modules/ without an interactive prompt."""
        with self._chdir_tmp() as tmp:
            apm_modules = self._create_fake_apm_modules(tmp)

            result = self.runner.invoke(cli, ["deps", "clean", "--yes"])

            assert result.exit_code == 0
            assert not apm_modules.exists(), "apm_modules/ must be removed when --yes is used"

    def test_yes_short_flag_skips_confirmation(self):
        """-y short flag must also skip confirmation."""
        with self._chdir_tmp() as tmp:
            apm_modules = self._create_fake_apm_modules(tmp)

            result = self.runner.invoke(cli, ["deps", "clean", "-y"])

            assert result.exit_code == 0
            assert not apm_modules.exists()

    def test_no_apm_modules_reports_already_clean(self):
        """When apm_modules/ does not exist the command should exit cleanly."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["deps", "clean"])

            assert result.exit_code == 0
            assert "already clean" in result.output

    def test_dry_run_no_apm_modules_reports_already_clean(self):
        """--dry-run with no apm_modules/ should also exit cleanly."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["deps", "clean", "--dry-run"])

            assert result.exit_code == 0
            assert "already clean" in result.output
