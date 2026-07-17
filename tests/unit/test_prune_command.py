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
import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconcileResult,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.cleanup import remove_stale_deployed_files
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
    if "lockfile_version:" not in yaml_content:
        yaml_content = "lockfile_version: '1'\n" + yaml_content
    lockfile_path.write_text(yaml_content)
    return lockfile_path


def _deployment_record(
    value: str,
    *,
    owners: tuple[str, ...],
    active_owner: str,
    content_hash: str | None = None,
) -> DeploymentRecord:
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=value,
        runtime=None,
        scope="project",
    )
    return DeploymentRecord(
        locator=locator,
        owners=owners,
        active_owner=active_owner,
        content_hash=content_hash,
    )


def _write_canonical_lockfile(
    root: Path,
    *,
    dependencies: dict[str, LockedDependency],
    records: tuple[DeploymentRecord, ...],
) -> Path:
    lockfile = LockFile(dependencies=dependencies)
    ledger = DeploymentLedger(records={record.locator.key: record for record in records})
    DeploymentLedgerCodec.apply_to_lockfile(ledger, lockfile)
    lockfile_path = root / "apm.lock.yaml"
    lockfile.write(lockfile_path)
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

    def test_prune_removes_real_orphan_with_sibling_subdir_dep(self):
        """Regression: the destructive ``apm prune`` command must
        delete a genuinely orphaned ``owner/repo`` package even when
        a sibling subdirectory dep ``owner/repo/.apm/skills/foo`` is
        declared in apm.yml.

        Previously, ``prune.py`` called ``_expand_with_ancestors``
        without the ``standalone_installed`` guard, so ``owner/repo``
        was added to the expected set as an ancestor of the subdir
        dep -- silently suppressing deletion of a real orphan and
        diverging from the advisory display path. ``apm prune`` is a
        safety command; missing a real orphan is a correctness bug.
        """
        with self._chdir_tmp() as tmp:
            # Declare ONLY the subdirectory dep. The standalone
            # owner/repo package is not declared anywhere.
            (tmp / "apm.yml").write_text(
                "name: test\n"
                "version: 1.0.0\n"
                "dependencies:\n"
                "  apm:\n"
                "    - git: github.example.com/owner/repo\n"
                "      path: .apm/skills/foo\n"
            )
            # Real installed standalone package (apm.yml + .apm marker).
            pkg_dir = tmp / "apm_modules" / "owner" / "repo"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "apm.yml").write_text("name: repo\nversion: 1.0\n")
            # Subdirectory dep content cohabits the same install root.
            skill_dir = pkg_dir / ".apm" / "skills" / "foo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Skill\n")

            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0, result.output
            # Real orphan MUST be deleted -- this is the security
            # invariant the panel flagged as a required fix.
            assert not (pkg_dir / "apm.yml").exists(), (
                "Real orphan owner/repo (apm.yml) must be removed even "
                "when a sibling subdir dep shares the same root"
            )
            # Subdir dep content collateral-damages because the whole
            # owner/repo tree is the orphan's filesystem footprint;
            # the user is expected to re-install. This matches the
            # advisory display path in deps/cli.py.
            assert not skill_dir.exists()

    def test_prune_dry_run_lists_real_orphan_with_sibling_subdir_dep(self):
        """Dry-run path must also surface the real orphan (display
        parity with the advisory check).
        """
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(
                "name: test\n"
                "version: 1.0.0\n"
                "dependencies:\n"
                "  apm:\n"
                "    - git: github.example.com/owner/repo\n"
                "      path: .apm/skills/foo\n"
            )
            pkg_dir = tmp / "apm_modules" / "owner" / "repo"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "apm.yml").write_text("name: repo\nversion: 1.0\n")
            skill_dir = pkg_dir / ".apm" / "skills" / "foo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Skill\n")

            result = self.runner.invoke(cli, ["prune", "--dry-run"])
            assert result.exit_code == 0, result.output
            assert "owner/repo" in result.output
            # No deletion occurred.
            assert (pkg_dir / "apm.yml").exists()

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

    def test_prune_repairs_ghost_owner_without_deleting_existing_bytes(self):
        """A ghost row is repairable metadata, never deletion authority."""
        ghost_path = ".github/prompts/ghost.prompt.md"
        alpha_path = ".github/prompts/alpha.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            ghost = tmp / ghost_path
            ghost.parent.mkdir(parents=True)
            ghost.write_text("# Manual sentinel\n")
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={},
                records=(
                    _deployment_record(
                        ghost_path,
                        owners=("removed/beta",),
                        active_owner="removed/beta",
                    ),
                    _deployment_record(
                        alpha_path,
                        owners=(".",),
                        active_owner=".",
                    ),
                ),
            )

            result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0, result.output
            assert ghost.read_text() == "# Manual sentinel\n"
            assert "without deleting untrusted bytes" in result.output
            repaired = LockFile.read(lockfile_path)
            assert repaired is not None
            assert DeploymentLedgerCodec.owner_reference_violations(repaired) == ()
            assert {
                record.locator.value for record in repaired.deployment_ledger.records.values()
            } == {alpha_path}

    def test_prune_repairs_missing_ghost_without_deletion_attempt(self):
        """A missing ghost path is metadata-only repair."""
        ghost_path = ".github/prompts/missing-ghost.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={},
                records=(
                    _deployment_record(
                        ghost_path,
                        owners=("removed/beta",),
                        active_owner="removed/beta",
                    ),
                ),
            )

            with patch(
                "apm_cli.commands.prune.remove_stale_deployed_files",
                wraps=remove_stale_deployed_files,
            ) as cleanup:
                result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0, result.output
            assert cleanup.call_count == 0
            assert not (tmp / ghost_path).exists()
            assert not lockfile_path.exists()

    def test_prune_cleans_trusted_path_but_preserves_unrelated_ghost(self):
        """Current-run claims may delete beta bytes, never ghost-only bytes."""
        beta_key = "orphan-org/orphan-repo"
        beta_path = ".github/prompts/beta.prompt.md"
        ghost_path = ".github/prompts/ghost.prompt.md"
        beta_content = b"# Managed beta\n"
        beta_hash = f"sha256:{hashlib.sha256(beta_content).hexdigest()}"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            beta = tmp / beta_path
            beta.parent.mkdir(parents=True)
            beta.write_bytes(beta_content)
            ghost = tmp / ghost_path
            ghost.write_text("# Manual sentinel\n")
            _write_canonical_lockfile(
                tmp,
                dependencies={
                    beta_key: LockedDependency(
                        repo_url=beta_key,
                        deployed_files=[beta_path],
                        deployed_file_hashes={beta_path: beta_hash},
                    )
                },
                records=(
                    _deployment_record(
                        beta_path,
                        owners=(beta_key,),
                        active_owner=beta_key,
                        content_hash=beta_hash,
                    ),
                    _deployment_record(
                        ghost_path,
                        owners=("removed/ghost",),
                        active_owner="removed/ghost",
                    ),
                ),
            )

            with patch(
                "apm_cli.commands.prune.remove_stale_deployed_files",
                wraps=remove_stale_deployed_files,
            ) as cleanup:
                result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0, result.output
            assert not beta.exists()
            assert ghost.read_text() == "# Manual sentinel\n"
            assert cleanup.call_count == 1
            assert cleanup.call_args.args[0] == {beta_path}
            assert ghost_path not in cleanup.call_args.args[0]
            assert "without deleting untrusted bytes" in result.output

    def test_prune_dry_run_does_not_repair_ghost_owner(self):
        """Dry-run reports a ghost repair without changing lockfile or bytes."""
        ghost_path = ".github/prompts/ghost.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            ghost = tmp / ghost_path
            ghost.parent.mkdir(parents=True)
            ghost.write_text("# Manual sentinel\n")
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={},
                records=(
                    _deployment_record(
                        ghost_path,
                        owners=("removed/beta",),
                        active_owner="removed/beta",
                    ),
                ),
            )
            before = lockfile_path.read_bytes()

            result = self.runner.invoke(cli, ["prune", "--dry-run"])

            assert result.exit_code == 0
            assert "repair 1 deployment ownership record" in result.output
            assert lockfile_path.read_bytes() == before
            assert ghost.read_text() == "# Manual sentinel\n"

    def test_prune_hands_shared_row_to_surviving_owner(self):
        """Removing a ghost co-owner preserves the survivor and content."""
        shared_path = ".github/prompts/shared.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            shared = tmp / shared_path
            shared.parent.mkdir(parents=True)
            shared.write_text("# Shared\n")
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={"kept/alpha": LockedDependency(repo_url="kept/alpha")},
                records=(
                    _deployment_record(
                        shared_path,
                        owners=("kept/alpha", "removed/beta"),
                        active_owner="removed/beta",
                        content_hash="sha256:shared",
                    ),
                ),
            )

            result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0
            assert shared.read_text() == "# Shared\n"
            repaired = LockFile.read(lockfile_path)
            assert repaired is not None
            record = next(iter(repaired.deployment_ledger.records.values()))
            assert record.owners == ("kept/alpha",)
            assert record.active_owner == "kept/alpha"
            assert record.content_hash == "sha256:shared"

    def test_prune_preserves_user_edited_file_but_removes_departed_owner(self):
        """An orphan's edited file becomes unmanaged instead of being deleted."""
        beta_key = "orphan-org/orphan-repo"
        deployed_path = ".github/prompts/orphan.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            deployed = tmp / deployed_path
            deployed.parent.mkdir(parents=True)
            deployed.write_text("# User edit\n")
            _write_canonical_lockfile(
                tmp,
                dependencies={
                    beta_key: LockedDependency(
                        repo_url=beta_key,
                        deployed_files=[deployed_path],
                        deployed_file_hashes={deployed_path: f"sha256:{'0' * 64}"},
                    )
                },
                records=(
                    _deployment_record(
                        deployed_path,
                        owners=(beta_key,),
                        active_owner=beta_key,
                        content_hash=f"sha256:{'0' * 64}",
                    ),
                ),
            )

            result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0, result.output
            assert deployed.read_text() == "# User edit\n"
            assert "edited since APM deployed it" in result.output
            lockfile_path = tmp / "apm.lock.yaml"
            if lockfile_path.exists():
                repaired = LockFile.read(lockfile_path)
                assert repaired is not None
                assert beta_key not in repaired.dependencies
                assert DeploymentLedgerCodec.owner_reference_violations(repaired) == ()

    def test_prune_keeps_owner_when_module_removal_fails(self):
        """A failed module removal cannot authorize owner reconciliation."""
        beta_key = "bad-org/bad-repo"
        deployed_path = ".github/prompts/bad.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "bad-org", "bad-repo")
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={
                    beta_key: LockedDependency(
                        repo_url=beta_key,
                        deployed_files=[deployed_path],
                    )
                },
                records=(
                    _deployment_record(
                        deployed_path,
                        owners=(beta_key,),
                        active_owner=beta_key,
                    ),
                ),
            )

            with patch(
                "apm_cli.commands.prune.safe_rmtree",
                side_effect=OSError("permission denied"),
            ):
                result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0
            retained = LockFile.read(lockfile_path)
            assert retained is not None
            assert beta_key in retained.dependencies
            assert DeploymentLedgerCodec.owner_reference_violations(retained) == ()

    def test_prune_codec_delegation_is_load_bearing(self):
        """Neutralizing codec reconciliation recreates the stale owner defect."""
        beta_key = "orphan-org/orphan-repo"
        beta_path = ".github/prompts/beta.prompt.md"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            lockfile_path = _write_canonical_lockfile(
                tmp,
                dependencies={beta_key: LockedDependency(repo_url=beta_key)},
                records=(
                    _deployment_record(
                        beta_path,
                        owners=(beta_key,),
                        active_owner=beta_key,
                    ),
                ),
            )
            original = LockFile.read(lockfile_path)
            assert original is not None
            no_op = DeploymentReconcileResult(
                ledger=original.deployment_ledger,
                removed=(),
                retained=(),
                owner_handoffs=(),
                failed=(),
                changed=False,
            )

            with patch.object(
                DeploymentLedgerCodec,
                "reconcile_owner_references",
                return_value=no_op,
            ):
                result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 0, result.output
            mutated = LockFile.read(lockfile_path)
            assert mutated is not None
            violations = DeploymentLedgerCodec.owner_reference_violations(mutated)
            assert len(violations) == 1
            assert violations[0].invalid_owners == (beta_key,)

    def test_prune_lockfile_write_failure_exits_nonzero(self):
        """Lock persistence errors are visible after partial filesystem cleanup."""
        beta_key = "orphan-org/orphan-repo"
        alpha_key = "kept/alpha"
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(_APM_YML_NO_DEPS)
            _make_package_dir(tmp, "orphan-org", "orphan-repo")
            alpha_record = _deployment_record(
                ".github/prompts/alpha.prompt.md",
                owners=(alpha_key,),
                active_owner=alpha_key,
            )
            _write_canonical_lockfile(
                tmp,
                dependencies={
                    alpha_key: LockedDependency(repo_url=alpha_key),
                    beta_key: LockedDependency(repo_url=beta_key),
                },
                records=(alpha_record,),
            )

            with patch.object(
                LockFile,
                "write",
                side_effect=OSError("disk full"),
            ):
                result = self.runner.invoke(cli, ["prune"])

            assert result.exit_code == 1
            assert "Failed to update apm.lock.yaml" in result.output
            assert "Rerun 'apm prune'" in result.output

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

    # ------------------------------------------------------------------
    # Regression: devDependencies must not be pruned (#2033)
    # ------------------------------------------------------------------

    def test_prune_keeps_dev_dependency_packages(self):
        """Regression #2033: prune must not remove devDependencies.apm packages."""
        apm_yml_with_dev_dep = (
            "name: test-project\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm:\n"
            "    - declared-org/prod-pkg\n"
            "  mcp: []\n"
            "devDependencies:\n"
            "  apm:\n"
            "    - dev-org/dev-pkg\n"
        )
        with self._chdir_tmp() as tmp:
            (tmp / "apm.yml").write_text(apm_yml_with_dev_dep)
            prod_dir = _make_package_dir(tmp, "declared-org", "prod-pkg")
            dev_dir = _make_package_dir(tmp, "dev-org", "dev-pkg")
            orphan_dir = _make_package_dir(tmp, "orphan-org", "orphan-repo")
            result = self.runner.invoke(cli, ["prune"])
            assert result.exit_code == 0
            assert prod_dir.exists(), "Prod dependency must remain"
            assert dev_dir.exists(), "Dev dependency must remain"
            assert not orphan_dir.exists(), "Orphaned package must be removed"
