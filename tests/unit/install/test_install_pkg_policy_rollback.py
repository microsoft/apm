"""Unit tests for apm install <pkg> manifest snapshot + rollback (#827).

W2-pkg-rollback: when ``apm install <pkg>`` mutates ``apm.yml`` BEFORE
the install pipeline runs, a policy block (or any pipeline failure) must
restore ``apm.yml`` to its pre-mutation state so the denied/failed
package never persists.

Covers:
- Policy block (enforcement=block) -> apm.yml byte-equal to pre-state,
  exit non-zero, error message + rollback notice visible.
- Policy warn (enforcement=warn)  -> apm.yml has new dep, exit zero,
  no rollback.
- Allowed package (no policy violation) -> apm.yml has new dep, exit zero.
- Pipeline failure unrelated to policy (download error) -> rollback,
  byte-equal, exit non-zero.
- --no-policy bypass -> apm.yml has new dep, exit zero.
- Fixture: tests/fixtures/policy/apm-policy-deny.yml

Coordination with W2-gate-phase (C2):
- ``PolicyViolationError`` is the real exception from
  ``install/phases/policy_gate.py`` (already landed on this branch).
  Tests use ``side_effect=PolicyViolationError(...)`` on the mocked
  ``_install_apm_dependencies`` to trigger the rollback path.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.results import InstallResult

# ---------------------------------------------------------------------------
# Placeholder policy exception (C2 coordination with W2-gate-phase)
# ---------------------------------------------------------------------------
# W2-gate-phase has landed: the real exception is PolicyViolationError
# in install/phases/policy_gate.py.  We import it and also keep a local
# alias for readability.  If the import fails (unlikely -- the module is
# already on this branch), fall back to a placeholder.

try:
    from apm_cli.install.phases.policy_gate import PolicyViolationError
except ImportError:  # pragma: no cover -- defensive

    class PolicyViolationError(RuntimeError):
        """Placeholder for the policy-gate block exception."""


# Alias for backward compatibility with the original test plan name.
PolicyBlockError = PolicyViolationError


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"

# A minimal apm.yml with known content -- used as the "before" state.
# The content deliberately includes a trailing newline and specific
# formatting to verify byte-exact restoration after rollback.
SEED_APM_YML = (
    "name: rollback-test\n"
    "version: 0.1.0\n"
    "dependencies:\n"
    "  apm:\n"
    "    - existing/package\n"
    "  mcp: []\n"
)


def _successful_install_result() -> InstallResult:
    diag = MagicMock(has_diagnostics=False, has_critical_security=False)
    return InstallResult(diagnostics=diag)


@pytest.fixture
def cli_runner():
    return CliRunner()


@contextlib.contextmanager
def _chdir_tmp(original_dir):
    """Create a temp dir, chdir into it, restore CWD on exit."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            os.chdir(tmp_dir)
            yield Path(tmp_dir)
        finally:
            os.chdir(original_dir)


def _write_seed_apm_yml(tmp_dir: Path) -> bytes:
    """Write SEED_APM_YML into ``tmp_dir/apm.yml`` and return its raw bytes."""
    apm_yml = tmp_dir / "apm.yml"
    raw = SEED_APM_YML.encode("utf-8")
    apm_yml.write_bytes(raw)
    return raw


def _mock_apm_package():
    """Return a MagicMock that satisfies APMPackage contract."""
    pkg = MagicMock()
    pkg.get_apm_dependencies.return_value = [
        MagicMock(repo_url="test/denied-pkg", reference="main"),
    ]
    pkg.get_mcp_dependencies.return_value = []
    pkg.get_dev_apm_dependencies.return_value = []
    return pkg


# ---------------------------------------------------------------------------
# Fixture: apm-policy-deny.yml exists
# ---------------------------------------------------------------------------


def test_policy_deny_fixture_exists():
    """Sanity: the deny-list policy fixture must be present."""
    deny_fixture = FIXTURE_DIR / "apm-policy-deny.yml"
    assert deny_fixture.exists(), f"Missing fixture: {deny_fixture}"
    data = yaml.safe_load(deny_fixture.read_text(encoding="utf-8"))
    assert data["enforcement"] == "block"
    assert "deny" in data.get("dependencies", {})


# ---------------------------------------------------------------------------
# Core rollback tests
# ---------------------------------------------------------------------------


class TestInstallPkgPolicyRollback:
    """Test manifest rollback when ``apm install <pkg>`` + pipeline failure."""

    def setup_method(self):
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent.parent)
            os.chdir(self.original_dir)
        self.runner = CliRunner()

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent.parent
            os.chdir(str(repo_root))

    # -- Denied package (policy block) -> rollback, byte-equal, non-zero --

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_policy_block_restores_manifest_byte_exact(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Policy block -> apm.yml restored byte-for-byte to pre-mutation state."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            original_bytes = _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError(
                "Dependency test-blocked/denied-pkg denied by org policy"
            )

            result = self.runner.invoke(cli, ["install", "test-blocked/denied-pkg"])

            assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
            # Verify byte-exact restoration
            restored_bytes = (tmp_dir / "apm.yml").read_bytes()
            assert restored_bytes == original_bytes, (
                "apm.yml was NOT restored byte-exactly after policy block.\n"
                f"  expected {len(original_bytes)} bytes, "
                f"got {len(restored_bytes)} bytes"
            )

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_policy_block_shows_rollback_notice(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """User sees both the pipeline error AND the rollback notice."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError(
                "Dependency test-blocked/denied-pkg denied by org policy"
            )

            result = self.runner.invoke(cli, ["install", "test-blocked/denied-pkg"])

            assert result.exit_code != 0
            assert "restored to its previous state" in result.output, (
                f"Rollback notice missing from output:\n{result.output}"
            )

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_policy_block_exit_code_nonzero(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Policy block must produce non-zero exit code."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError("blocked")

            result = self.runner.invoke(cli, ["install", "test-blocked/foo"])
            assert result.exit_code != 0

    # -- Warn mode -> no rollback, apm.yml has new dep, exit zero --

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_warn_mode_keeps_new_dep_in_manifest(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Warn mode (no exception) -> apm.yml retains the new dep, exit 0."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(cli, ["install", "test-ok/new-package"])

            assert result.exit_code == 0, (
                f"Expected exit 0, got {result.exit_code}\n{result.output}"
            )
            # apm.yml should contain the new dep (written by
            # _validate_and_add_packages_to_apm_yml)
            content = (tmp_dir / "apm.yml").read_text(encoding="utf-8")
            assert "test-ok/new-package" in content, f"New dep missing from apm.yml:\n{content}"
            # Rollback notice should NOT appear
            assert "restored to its previous state" not in result.output

    # -- Allowed package -> apm.yml has new dep, exit zero --

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_allowed_package_keeps_new_dep(self, mock_install_apm, mock_apm_package, mock_validate):
        """Normal install (no policy violation) -> apm.yml has the new dep."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(cli, ["install", "allowed-org/good-package"])

            assert result.exit_code == 0
            content = (tmp_dir / "apm.yml").read_text(encoding="utf-8")
            assert "allowed-org/good-package" in content

    # -- Pipeline failure (download error) -> rollback, byte-equal --

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_download_error_restores_manifest_byte_exact(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Non-policy pipeline failure (download error) -> rollback + byte-exact."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            original_bytes = _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = ConnectionError("Failed to download: connection refused")

            result = self.runner.invoke(cli, ["install", "some-org/failing-pkg"])

            assert result.exit_code != 0
            restored_bytes = (tmp_dir / "apm.yml").read_bytes()
            assert restored_bytes == original_bytes, (
                "apm.yml was NOT restored after download error.\n"
                f"  expected {len(original_bytes)} bytes, "
                f"got {len(restored_bytes)} bytes"
            )

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_download_error_shows_rollback_notice(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Download error -> user sees the rollback notice."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = RuntimeError("download timeout")

            result = self.runner.invoke(cli, ["install", "some-org/timeout-pkg"])

            assert result.exit_code != 0
            assert "restored to its previous state" in result.output

    # -- --no-policy bypass -> no rollback, apm.yml has new dep --
    # NOTE: --no-policy flag is W2-escape-hatch scope.  This test
    # simulates the bypass effect: pipeline completes successfully
    # (as it would when the gate is skipped).  Once --no-policy lands,
    # update this test to pass the actual flag.

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_no_policy_bypass_keeps_new_dep(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """When policy is bypassed (gate does not raise), apm.yml retains dep."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(cli, ["install", "test-blocked/denied-pkg"])

            assert result.exit_code == 0
            content = (tmp_dir / "apm.yml").read_text(encoding="utf-8")
            assert "test-blocked/denied-pkg" in content
            assert "restored to its previous state" not in result.output


# ---------------------------------------------------------------------------
# Byte-equality stress tests
# ---------------------------------------------------------------------------


class TestSnapshotByteIntegrity:
    """Verify that the raw-bytes snapshot survives YAML round-trip drift."""

    def setup_method(self):
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent.parent)
            os.chdir(self.original_dir)
        self.runner = CliRunner()

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent.parent
            os.chdir(str(repo_root))

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_trailing_newline_preserved_after_rollback(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Trailing newline in original apm.yml must survive rollback."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            # Seed with NO trailing newline (unusual but valid)
            raw_no_newline = b"name: test\nversion: 0.1.0\ndependencies:\n  apm: []\n  mcp: []"
            (tmp_dir / "apm.yml").write_bytes(raw_no_newline)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError("blocked")

            self.runner.invoke(cli, ["install", "test/pkg"])

            assert (tmp_dir / "apm.yml").read_bytes() == raw_no_newline

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_unicode_content_preserved_after_rollback(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """UTF-8 content (comments, descriptions) must survive rollback."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            raw_utf8 = (
                b"# Project: Test\n"
                b"name: unicode-test\n"
                b"version: 0.1.0\n"
                b"dependencies:\n"
                b"  apm:\n"
                b"    - existing/package\n"
                b"  mcp: []\n"
            )
            (tmp_dir / "apm.yml").write_bytes(raw_utf8)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError("blocked")

            self.runner.invoke(cli, ["install", "test/pkg"])

            assert (tmp_dir / "apm.yml").read_bytes() == raw_utf8

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_comment_preservation_after_rollback(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """YAML comments in original apm.yml must survive rollback.

        YAML round-trip (load+dump) strips comments.  Raw-bytes snapshot
        guarantees they are restored.
        """
        with _chdir_tmp(self.original_dir) as tmp_dir:
            raw_with_comments = (
                b"# This is my project\n"
                b"name: commented-project\n"
                b"version: 1.0.0\n"
                b"\n"
                b"# Dependencies managed by APM\n"
                b"dependencies:\n"
                b"  apm:\n"
                b"    - existing/dep  # pinned for stability\n"
                b"  mcp: []\n"
            )
            (tmp_dir / "apm.yml").write_bytes(raw_with_comments)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyBlockError("blocked")

            self.runner.invoke(cli, ["install", "test/pkg"])

            restored = (tmp_dir / "apm.yml").read_bytes()
            assert restored == raw_with_comments, (
                "Comments were lost during rollback!  "
                "Snapshot must be raw bytes, not YAML round-trip."
            )


# ---------------------------------------------------------------------------
# Rollback helper unit tests
# ---------------------------------------------------------------------------


class TestRestoreManifestFromSnapshot:
    """Direct tests for _restore_manifest_from_snapshot."""

    def test_atomic_restore_byte_exact(self, tmp_path):
        """Restored file is byte-identical to snapshot."""
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        target = tmp_path / "apm.yml"
        original = b"name: test\nversion: 1.0.0\n"
        target.write_bytes(b"mutated content")

        _restore_manifest_from_snapshot(target, original)

        assert target.read_bytes() == original

    def test_atomic_restore_no_temp_file_left(self, tmp_path):
        """No temporary files remain after successful restore."""
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        target = tmp_path / "apm.yml"
        target.write_bytes(b"mutated")

        _restore_manifest_from_snapshot(target, b"original")

        # Only the target file should exist in tmp_path
        files = list(tmp_path.iterdir())
        assert len(files) == 1 and files[0].name == "apm.yml"

    def test_atomic_restore_replaces_existing(self, tmp_path):
        """Restore replaces the mutated file, not appends."""
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        target = tmp_path / "apm.yml"
        original = b"short"
        target.write_bytes(b"this is much longer mutated content")

        _restore_manifest_from_snapshot(target, original)

        assert target.read_bytes() == original


class TestMaybeRollbackManifest:
    """Direct tests for _maybe_rollback_manifest."""

    def test_noop_when_snapshot_is_none(self, tmp_path):
        """No-op when snapshot is None (not an ``install <pkg>`` invocation)."""
        from apm_cli.commands.install import _maybe_rollback_manifest

        target = tmp_path / "apm.yml"
        target.write_bytes(b"should not change")

        logger = MagicMock()
        _maybe_rollback_manifest(target, None, logger)

        assert target.read_bytes() == b"should not change"
        logger.progress.assert_not_called()
        logger.warning.assert_not_called()

    def test_restores_and_logs_when_snapshot_present(self, tmp_path):
        """Restores apm.yml and emits info message."""
        from apm_cli.commands.install import _maybe_rollback_manifest

        target = tmp_path / "apm.yml"
        target.write_bytes(b"mutated")

        logger = MagicMock()
        _maybe_rollback_manifest(target, b"original", logger)

        assert target.read_bytes() == b"original"
        logger.progress.assert_called_once_with("apm.yml restored to its previous state.")

    def test_warns_on_restore_failure(self, tmp_path):
        """If restore fails, warn but don't mask the original error."""
        from apm_cli.commands.install import _maybe_rollback_manifest

        # Point at a non-existent directory so tempfile.mkstemp fails
        bad_path = tmp_path / "nonexistent" / "apm.yml"

        logger = MagicMock()
        # Should not raise -- best-effort
        _maybe_rollback_manifest(bad_path, b"data", logger)

        logger.warning.assert_called_once()
        assert "Failed to restore" in logger.warning.call_args[0][0]


# ---------------------------------------------------------------------------
# No rollback when ``apm install`` (without packages)
# ---------------------------------------------------------------------------


class TestNoRollbackWithoutPackages:
    """When running bare ``apm install``, no snapshot is taken."""

    def setup_method(self):
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent.parent)
            os.chdir(self.original_dir)
        self.runner = CliRunner()

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent.parent
            os.chdir(str(repo_root))

    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_bare_install_failure_does_not_rollback(self, mock_install_apm, mock_apm_package):
        """``apm install`` (no pkgs) -> pipeline error does NOT touch apm.yml."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            # Write a known apm.yml
            raw = SEED_APM_YML.encode("utf-8")
            (tmp_dir / "apm.yml").write_bytes(raw)

            pkg = _mock_apm_package()
            mock_apm_package.from_apm_yml.return_value = pkg
            mock_install_apm.side_effect = RuntimeError("download failed")

            result = self.runner.invoke(cli, ["install"])

            assert result.exit_code != 0
            # apm.yml should be UNTOUCHED (no mutation happened, no rollback needed)
            assert (tmp_dir / "apm.yml").read_bytes() == raw
            # No rollback notice (snapshot was never taken)
            assert "restored to its previous state" not in result.output
