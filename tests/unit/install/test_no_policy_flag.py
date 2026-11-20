"""Unit tests for the --no-policy escape hatch (W2-escape-hatch, #827).

Tests the user-facing CLI surface: ``--no-policy`` flag on ``apm install``
(bare, with <pkg>, with --mcp) and ``apm update``, plus the
``APM_POLICY_DISABLE=1`` env var.

Covers:
- ``apm install --no-policy`` against denied dep (block mode) -> proceeds, exit 0, loud warning
- ``apm install <pkg> --no-policy`` -> apm.yml gets the new dep (no rollback)
- ``apm install --mcp <denied> --no-policy`` -> proceeds
- ``apm update --no-policy`` -> proceeds (flag accepted)
- ``APM_POLICY_DISABLE=1`` env var ALONE (no flag) -> same skip as --no-policy
- ``APM_POLICY_DISABLE=0`` or unset -> normal enforcement
- Without --no-policy AND denied dep AND block mode -> install fails (sanity)
- Loud warnings show even without ``-v`` / ``--verbose``
- Help text validation: ``--no-policy`` appears in ``install --help`` and ``update --help``
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.results import InstallResult

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A minimal apm.yml that has no deps -- used for bare-install and add-pkg tests.
SEED_APM_YML = (
    "name: no-policy-test\n"
    "version: 0.1.0\n"
    "dependencies:\n"
    "  apm:\n"
    "    - existing/package\n"
    "  mcp: []\n"
)


def _successful_install_result() -> InstallResult:
    diag = MagicMock(has_diagnostics=False, has_critical_security=False, error_count=0)
    return InstallResult(diagnostics=diag)


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
    """Write SEED_APM_YML into ``tmp_dir/apm.yml`` and return raw bytes."""
    apm_yml = tmp_dir / "apm.yml"
    raw = SEED_APM_YML.encode("utf-8")
    apm_yml.write_bytes(raw)
    return raw


def _mock_apm_package():
    """Return a MagicMock that satisfies APMPackage contract."""
    pkg = MagicMock()
    pkg.get_apm_dependencies.return_value = [
        MagicMock(repo_url="existing/package", reference="main"),
    ]
    pkg.get_mcp_dependencies.return_value = []
    pkg.get_dev_apm_dependencies.return_value = []
    return pkg


# Import the real PolicyViolationError from the gate phase.
try:
    from apm_cli.install.phases.policy_gate import PolicyViolationError
except ImportError:  # pragma: no cover

    class PolicyViolationError(RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Help text validation
# ---------------------------------------------------------------------------


class TestHelpTextShowsNoPolicy:
    """--no-policy must appear with the correct help text in --help output."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_install_help_shows_no_policy(self):
        result = self.runner.invoke(cli, ["install", "--help"])
        assert result.exit_code == 0
        assert "--no-policy" in result.output
        assert "Skip org policy enforcement" in result.output
        # Click wraps long help text across lines; normalize whitespace.
        normalized = " ".join(result.output.split())
        assert "Does NOT bypass apm audit --ci" in normalized

    def test_update_help_does_not_show_no_policy(self):
        """`--no-policy` is intentionally NOT exposed on `apm update` (CLI self-update)."""
        result = self.runner.invoke(cli, ["update", "--help"])
        assert result.exit_code == 0
        assert "--no-policy" not in result.output

    def test_install_help_lists_kiro_runtime_and_global_scope(self):
        result = self.runner.invoke(cli, ["install", "--help"])
        assert result.exit_code == 0
        normalized = " ".join(result.output.split())
        assert "Target a specific runtime only." in normalized
        assert "intellij" in normalized
        assert "kiro" in normalized
        # S11 (#3967): -g help keeps no second runtime list; the skip warning
        # names the supported runtimes, derived from the adapters.
        from apm_cli.integration.mcp_user_scope_runtimes import user_scope_runtime_labels

        assert "skipped with a warning that lists the supported ones" in normalized
        assert "Kiro" in user_scope_runtime_labels()
        assert "Kiro, Windsurf" not in normalized

    def test_help_text_is_plain_ascii(self):
        """Help text must be plain ASCII per cli.instructions.md."""
        result = self.runner.invoke(cli, ["install", "--help"])
        assert result.output.isascii(), f"Non-ASCII characters in install --help output"  # noqa: F541
        result = self.runner.invoke(cli, ["update", "--help"])
        assert result.output.isascii(), f"Non-ASCII characters in update --help output"  # noqa: F541


# ---------------------------------------------------------------------------
# apm install --no-policy (bare install -- denied dep in block mode)
# ---------------------------------------------------------------------------


class TestInstallNoPolicyFlag:
    """--no-policy causes policy_gate to skip, allowing denied deps."""

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
    def test_no_policy_flag_proceeds_on_denied_dep(self, mock_install_apm, mock_apm_package):
        """apm install --no-policy -> install proceeds (exit 0) even with denied dep."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(cli, ["install", "--no-policy"])

            assert result.exit_code == 0, (
                f"Expected exit 0 with --no-policy, got {result.exit_code}\nOutput: {result.output}"
            )
            # Verify no_policy=True was passed through to _install_apm_dependencies
            mock_install_apm.assert_called_once()
            call_kwargs = mock_install_apm.call_args
            assert call_kwargs.kwargs.get("no_policy") is True or (
                len(call_kwargs.args) > 0 and "no_policy" in str(call_kwargs)
            ), "no_policy=True must be passed to _install_apm_dependencies"

    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_no_policy_passes_through_to_install_apm_deps(self, mock_install_apm, mock_apm_package):
        """Verify no_policy=True kwarg reaches _install_apm_dependencies."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            self.runner.invoke(cli, ["install", "--no-policy"])

            _, kwargs = mock_install_apm.call_args
            assert kwargs["no_policy"] is True

    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_without_no_policy_default_is_false(self, mock_install_apm, mock_apm_package):
        """Without --no-policy, no_policy=False is passed through."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            self.runner.invoke(cli, ["install"])

            _, kwargs = mock_install_apm.call_args
            assert kwargs["no_policy"] is False


# ---------------------------------------------------------------------------
# apm install <pkg> --no-policy -> apm.yml retains the new dep
# ---------------------------------------------------------------------------


class TestInstallPkgNoPolicy:
    """apm install <pkg> --no-policy -> new dep stays in apm.yml (no rollback)."""

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
    def test_install_pkg_no_policy_keeps_dep_in_manifest(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """apm install <pkg> --no-policy succeeds; apm.yml has the new dep."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            original_bytes = _write_seed_apm_yml(tmp_dir)  # noqa: F841

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(cli, ["install", "test-blocked/denied-pkg", "--no-policy"])

            assert result.exit_code == 0, (
                f"Expected exit 0 with --no-policy, got {result.exit_code}\nOutput: {result.output}"
            )
            # Verify no_policy was passed as True
            _, kwargs = mock_install_apm.call_args
            assert kwargs["no_policy"] is True


# ---------------------------------------------------------------------------
# apm install --mcp <denied> --no-policy -> proceeds
# ---------------------------------------------------------------------------


class TestInstallMcpNoPolicy:
    """install --mcp --no-policy skips MCP preflight policy check."""

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

    @patch("apm_cli.policy.install_preflight.run_policy_preflight")
    @patch("apm_cli.commands.install._run_mcp_install")
    def test_mcp_no_policy_passes_flag_to_preflight(self, mock_run_mcp, mock_preflight):
        """--no-policy is forwarded to run_policy_preflight as no_policy=True."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_preflight.return_value = (None, False)

            self.runner.invoke(
                cli,
                [
                    "install",
                    "--mcp",
                    "test-server",
                    "--url",
                    "https://example.com/mcp",
                    "--target",
                    "claude",
                    "--no-policy",
                ],
            )

            mock_preflight.assert_called_once()
            _, kwargs = mock_preflight.call_args
            assert kwargs["no_policy"] is True

    @patch("apm_cli.policy.install_preflight.run_policy_preflight")
    @patch("apm_cli.commands.install._run_mcp_install")
    def test_mcp_without_no_policy_passes_false(self, mock_run_mcp, mock_preflight):
        """Without --no-policy, no_policy=False is forwarded to preflight."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_preflight.return_value = (None, False)

            self.runner.invoke(
                cli,
                [
                    "install",
                    "--mcp",
                    "test-server",
                    "--url",
                    "https://example.com/mcp",
                    "--target",
                    "claude",
                ],
            )

            mock_preflight.assert_called_once()
            _, kwargs = mock_preflight.call_args
            assert kwargs["no_policy"] is False


# ---------------------------------------------------------------------------
# apm update --no-policy -> rejected (apm update is CLI self-update, not deps)
# ---------------------------------------------------------------------------


class TestUpdateNoPolicy:
    """apm update intentionally does NOT accept --no-policy."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_update_no_policy_flag_rejected(self):
        """`apm update --no-policy` exits non-zero with usage error.

        ``apm update`` is the dependency-graph refresh command (since
        issue #1203). The CLI self-updater moved to ``apm self-update``.
        ``apm update`` does not surface ``--no-policy`` because policy
        enforcement is fixed-on for the refresh flow; users who need the
        opt-out must use ``apm install --update --no-policy``.
        """
        result = self.runner.invoke(cli, ["update", "--no-policy"])
        assert result.exit_code != 0, f"Expected non-zero exit, got 0\nOutput: {result.output}"


# ---------------------------------------------------------------------------
# APM_POLICY_DISABLE=1 env var (no --no-policy flag)
# ---------------------------------------------------------------------------


class TestEnvVarPolicyDisable:
    """APM_POLICY_DISABLE=1 env var alone triggers the same skip as --no-policy."""

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
    def test_env_var_disable_proceeds(self, mock_install_apm, mock_apm_package):
        """APM_POLICY_DISABLE=1 (no flag) -> install proceeds normally."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(
                cli,
                ["install"],
                env={"APM_POLICY_DISABLE": "1"},
            )

            assert result.exit_code == 0, (
                f"Expected exit 0 with APM_POLICY_DISABLE=1, got {result.exit_code}\n"
                f"Output: {result.output}"
            )

    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_env_var_zero_does_not_disable(self, mock_install_apm, mock_apm_package):
        """APM_POLICY_DISABLE=0 -> normal enforcement (no skip)."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.return_value = _successful_install_result()

            result = self.runner.invoke(  # noqa: F841
                cli,
                ["install"],
                env={"APM_POLICY_DISABLE": "0"},
            )

            # Normal invocation -- no_policy should be False
            _, kwargs = mock_install_apm.call_args
            assert kwargs["no_policy"] is False


# ---------------------------------------------------------------------------
# Sanity: without --no-policy AND denied dep -> install fails
# ---------------------------------------------------------------------------


class TestWithoutNoPolicyDeniedDepFails:
    """Without --no-policy, a PolicyViolationError causes non-zero exit."""

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
    def test_denied_dep_without_no_policy_fails(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Normal enforcement: denied dep causes non-zero exit."""
        with _chdir_tmp(self.original_dir) as tmp_dir:
            _write_seed_apm_yml(tmp_dir)

            mock_validate.return_value = True
            mock_apm_package.from_apm_yml.return_value = _mock_apm_package()
            mock_install_apm.side_effect = PolicyViolationError(
                "Dependency test-blocked/denied-pkg denied by org policy"
            )

            result = self.runner.invoke(cli, ["install", "test-blocked/denied-pkg"])

            assert result.exit_code != 0, (
                f"Expected non-zero exit without --no-policy, got {result.exit_code}"
            )


# ---------------------------------------------------------------------------
# Loud warnings visible even without --verbose
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Policy gate unit test: ctx.no_policy + env var skip enforcement
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Install preflight (--mcp) escape hatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# InstallRequest and pipeline wiring
# ---------------------------------------------------------------------------
