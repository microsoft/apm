"""Unit tests for the apm prune command.

Tests cover:
- Missing apm.yml
- Missing apm_modules/ directory
- Clean state (no orphaned packages)
- Orphaned packages with --dry-run
- Orphaned packages removal
- Parse error in apm.yml
- safe_rmtree failure handling
- Lockfile cleanup for pruned packages with deployed files
- Lockfile deletion when all entries are removed
"""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_APM_YML_NO_DEPS = """\
name: test-project
version: 1.0.0
dependencies:
  apm: []
  mcp: []
"""

_APM_YML_WITH_DEP = """\
name: test-project
version: 1.0.0
dependencies:
  apm:
    - declared-org/declared-repo
  mcp: []
"""


def _make_package_dir(root: Path, org: str, repo: str) -> Path:
    """Create an installed package directory with an apm.yml marker."""
    pkg_dir = root / "apm_modules" / org / repo
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "apm.yml").write_text(f"name: {repo}\nversion: 1.0\n")
    return pkg_dir


def _write_lockfile(root: Path, yaml_content: str) -> Path:
    """Write an apm.lock.yaml file at *root* (current lockfile format)."""
    lockfile_path = root / "apm.lock.yaml"
    lockfile_path.write_text(yaml_content)
    return lockfile_path


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestPruneCommand:
    """Tests for ``apm prune``."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        clear_apm_yml_cache()
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
                clear_apm_yml_cache()

    # ------------------------------------------------------------------
    # Missing apm.yml
    # ------------------------------------------------------------------

    def test_no_apm_yml_exits_with_error(self):
        """prune must fail with exit 1 when apm.yml is absent."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 1
            assert "apm.yml" in result.output

    def test_no_apm_yml_dry_run_exits_with_error(self):
        """prune --dry-run must also fail when apm.yml is absent."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 1

    # ------------------------------------------------------------------
    # Missing apm_modules/
    # ------------------------------------------------------------------

    def test_no_apm_modules_dir_exits_cleanly(self):
        """prune exits 0 with info message when apm_modules/ does not exist."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert "Nothing to prune" in result.output or "apm_modules" in result.output

    # ------------------------------------------------------------------
    # Clean state - no orphaned packages
    # ------------------------------------------------------------------

    def test_no_orphaned_packages_reports_clean(self):
        """prune reports clean state when all installed packages are declared."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_WITH_DEP)
            _make_package_dir(tmp, "declared-org", "declared-repo")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert "No orphaned packages" in result.output

    def test_no_orphaned_packages_dry_run_also_reports_clean(self):
        """--dry-run reports clean state when nothing would be pruned."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_WITH_DEP)
            _make_package_dir(tmp, "declared-org", "declared-repo")
            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "No orphaned packages" in result.output

    # ------------------------------------------------------------------
    # Dry-run with orphaned packages
    # ------------------------------------------------------------------

    def test_dry_run_lists_orphans_without_removing(self):
        """--dry-run shows orphaned packages but leaves them on disk."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            orphan_dir = _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "orphan-org/orphan-repo" in result.output
            assert orphan_dir.exists(), "Package dir must NOT be removed in dry-run mode"

    def test_dry_run_says_no_changes_made(self):
        """--dry-run output should indicate no changes were made."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "Dry run" in result.output or "dry" in result.output.lower()

    def test_dry_run_multiple_orphans(self):
        """--dry-run lists all orphaned packages."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "org1", "repo1")
            _make_package_dir(tmp, "org2", "repo2")
            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 0
            assert "org1/repo1" in result.output
            assert "org2/repo2" in result.output

    # ------------------------------------------------------------------
    # Actual removal
    # ------------------------------------------------------------------

    def test_prune_removes_orphaned_package(self):
        """prune removes a package that is installed but not in apm.yml."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            orphan_dir = _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert not orphan_dir.exists(), "Orphaned package dir should be removed"

    def test_prune_keeps_declared_packages(self):
        """prune must not remove packages that are declared in apm.yml."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_WITH_DEP)
            declared_dir = _make_package_dir(tmp, "declared-org", "declared-repo")
            orphan_dir = _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert declared_dir.exists(), "Declared package must remain"
            assert not orphan_dir.exists(), "Orphaned package must be removed"

    def test_prune_reports_count_removed(self):
        """prune output should mention how many packages were removed."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            # Output should mention the removal (count or package name)
            assert "Pruned" in result.output or "orphan-org/orphan-repo" in result.output

    def test_prune_removes_multiple_orphans(self):
        """prune removes all orphaned packages in one pass."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            dir1 = _make_package_dir(tmp, "org1", "repo1")
            dir2 = _make_package_dir(tmp, "org2", "repo2")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert not dir1.exists()
            assert not dir2.exists()

    # ------------------------------------------------------------------
    # Parse error in apm.yml
    # ------------------------------------------------------------------

    def test_invalid_apm_yml_exits_with_error(self):
        """prune exits 1 when apm.yml cannot be parsed."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(":\tinvalid: yaml: content\n\t{broken")
            (tmp / "apm_modules").mkdir()
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 1

    # ------------------------------------------------------------------
    # safe_rmtree failure
    # ------------------------------------------------------------------

    def test_prune_handles_rmtree_failure_gracefully(self):
        """prune reports error for a package that cannot be removed and continues."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "bad-org", "bad-repo")

            with patch(
                "apm_cli.commands.prune.safe_rmtree",
                side_effect=OSError("permission denied"),
            ):
                result = self.runner.invoke(cli, ["prune"])

            # Command should continue gracefully and not fail the whole prune run
            assert result.exit_code == 0
            # Should report the failure (not crash silently)
            assert "bad-org/bad-repo" in result.output or "Failed" in result.output

    # ------------------------------------------------------------------
    # Lockfile cleanup
    # ------------------------------------------------------------------

    def test_prune_removes_lockfile_entry_for_pruned_package(self):
        """prune deletes the lockfile entry for a pruned package."""
        lockfile_content = """\
version: 1
dependencies:
  - repo_url: orphan-org/orphan-repo
    host: github.com
    resolved_commit: abc123
    depth: 1
"""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            _write_lockfile(tmp, lockfile_content)
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            lockfile_path = tmp / "apm.lock.yaml"
            # When the package is pruned, its lockfile entry should be removed;
            # the lockfile itself may also be deleted.
            if lockfile_path.exists():
                assert "orphan-org/orphan-repo" not in lockfile_path.read_text()
            else:
                pass

    def test_prune_removes_lockfile_entry_exact(self):
        """prune deletes apm.lock.yaml when it only contained the pruned package."""
        lockfile_content = """\
version: 1
dependencies:
  - repo_url: orphan-org/orphan-repo
    host: github.com
    resolved_commit: abc123
    depth: 1
"""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            _write_lockfile(tmp, lockfile_content)
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            lockfile_path = tmp / "apm.lock.yaml"
            # When all packages are pruned, lockfile should be removed or not contain the entry
            if lockfile_path.exists():
                assert "orphan-org/orphan-repo" not in lockfile_path.read_text()
            else:
                pass  # deleted - also acceptable

    def test_prune_cleans_deployed_files_from_lockfile(self):
        """prune removes deployed integration files listed in the lockfile."""
        lockfile_content = """\
version: 1
dependencies:
  - repo_url: orphan-org/orphan-repo
    host: github.com
    resolved_commit: abc123
    depth: 1
    deployed_files:
      - .github/prompts/orphan-prompt.md
"""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            _write_lockfile(tmp, lockfile_content)
            # Create the deployed file
            deployed = tmp / ".github" / "prompts" / "orphan-prompt.md"
            deployed.parent.mkdir(parents=True, exist_ok=True)
            deployed.write_text("# Orphan prompt\n")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert not deployed.exists(), "Deployed file must be removed by prune"

    def test_prune_deletes_lockfile_when_empty(self):
        """prune deletes apm.lock.yaml entirely when all dependencies are pruned."""
        lockfile_content = """\
version: 1
dependencies:
  - repo_url: orphan-org/orphan-repo
    host: github.com
    resolved_commit: abc123
    depth: 1
"""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            _write_lockfile(tmp, lockfile_content)
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert not (tmp / "apm.lock.yaml").exists(), (
                "apm.lock.yaml should be deleted when empty"
            )

    def test_prune_preserves_lockfile_for_remaining_packages(self):
        """prune keeps lockfile entries for packages that are NOT pruned."""
        lockfile_content = """\
version: 1
dependencies:
  - repo_url: declared-org/declared-repo
    host: github.com
    resolved_commit: abc123
    depth: 1
  - repo_url: orphan-org/orphan-repo
    host: github.com
    resolved_commit: def456
    depth: 1
"""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_WITH_DEP)
            _make_package_dir(tmp, "declared-org", "declared-repo")
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            _write_lockfile(tmp, lockfile_content)
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            lockfile_path = tmp / "apm.lock.yaml"
            assert lockfile_path.exists(), "lockfile should remain for kept packages"
            content = lockfile_path.read_text()
            assert "declared-org/declared-repo" in content
            assert "orphan-org/orphan-repo" not in content

    # ------------------------------------------------------------------
    # No lockfile present
    # ------------------------------------------------------------------

    def test_prune_works_without_lockfile(self):
        """prune removes orphaned packages even when no apm.lock.yaml exists."""
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            orphan_dir = _make_package_dir(tmp, "orphan-org", "orphan-repo")
            # No apm.lock created
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert not orphan_dir.exists()
