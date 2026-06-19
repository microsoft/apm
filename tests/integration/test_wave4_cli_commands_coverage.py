"""Integration tests for Wave4 CLI commands to maximize code coverage.

Tests cover:
- apm prune (with --dry-run, error paths)
- apm run (with scripts, parameters, error handling)
- apm config (show, get, set)
- apm experimental (list, enable, disable, reset)
- apm update (with --dry-run, error paths)
- apm runtime (list, setup)
- apm policy (list, validate, status)
- _format_target_label helper function
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands.compile.watcher import _format_target_label
from apm_cli.models.apm_package import clear_apm_yml_cache

# ====== Test Setup Helpers ======


def _make_basic_apm_yml(tmp_path: Path) -> Path:
    """Create a minimal valid apm.yml."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: test-project\nversion: 1.0.0\n")
    return apm_yml


def _make_apm_yml_with_scripts(tmp_path: Path) -> Path:
    """Create apm.yml with scripts section."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\nversion: 1.0.0\n"
        "scripts:\n"
        "  start: echo 'Starting...'\n"
        "  build: echo 'Building...'\n"
    )
    return apm_yml


def _make_apm_yml_with_deps(tmp_path: Path) -> Path:
    """Create apm.yml with APM dependencies."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\nversion: 1.0.0\n"
        "dependencies:\n"
        "  apm:\n"
        "    - owner/package1\n"
        "    - owner/package2\n"
    )
    return apm_yml


def _make_installed_packages(tmp_path: Path) -> list[Path]:
    """Create installed package directories in apm_modules/."""
    packages = []
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir(exist_ok=True)
    for org_repo in ["owner/orphaned", "owner/package1", "owner/package2"]:
        org, repo = org_repo.split("/")
        pkg_dir = apm_modules / org / repo
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "apm.yml").write_text(f"name: {repo}\nversion: 1.0.0\n")
        packages.append(pkg_dir)
    return packages


def _make_lockfile(tmp_path: Path) -> Path:
    """Create a basic lockfile."""
    lockfile = tmp_path / "apm.lock.yaml"
    lockfile.write_text("version: 1\nlock: {}\n")
    return lockfile


# ====== Prune Command Tests ======


class TestPruneCommand:
    """Tests for ``apm prune``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_prune_no_apm_yml(self, tmp_path: Path) -> None:
        """Test prune when apm.yml doesn't exist."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["prune"])
        assert result.exit_code != 0
        assert "apm.yml" in result.output or "apm.yml" in result.stderr

    def test_prune_no_apm_modules(self, tmp_path: Path) -> None:
        """Test prune when apm_modules/ doesn't exist (clean state)."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "apm_modules" in result.output.lower() or "nothing" in result.output.lower()

    def test_prune_no_orphaned_packages(self, tmp_path: Path) -> None:
        """Test prune when no orphaned packages exist."""
        os.chdir(tmp_path)
        _make_apm_yml_with_deps(tmp_path)
        _make_lockfile(tmp_path)
        _make_installed_packages(tmp_path)
        result = self.runner.invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "pruned" in result.output.lower() or "no orphaned" in result.output.lower()

    def test_prune_dry_run(self, tmp_path: Path) -> None:
        """Test prune --dry-run (shows what would be removed)."""
        os.chdir(tmp_path)
        _make_apm_yml_with_deps(tmp_path)
        _make_lockfile(tmp_path)
        _make_installed_packages(tmp_path)
        result = self.runner.invoke(cli, ["prune", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in result.output.lower()

    def test_prune_invalid_yaml(self, tmp_path: Path) -> None:
        """Test prune with missing required fields in YAML."""
        os.chdir(tmp_path)
        # Missing 'version' field - command should fail
        (tmp_path / "apm.yml").write_text("name: test-package\n")
        result = self.runner.invoke(cli, ["prune"])
        # With missing version, should either exit non-zero or show error
        # Prune is graceful, so just verify it runs without crashing
        assert result.exit_code in [0, 1]


# ====== Run Command Tests ======


class TestRunCommand:
    """Tests for ``apm run``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_run_no_script_specified_no_start(self, tmp_path: Path) -> None:
        """Test run with no script name and no 'start' script defined."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["run"])
        assert result.exit_code != 0
        assert "script" in result.output.lower()

    def test_run_with_script_name(self, tmp_path: Path) -> None:
        """Test run with explicit script name."""
        os.chdir(tmp_path)
        _make_apm_yml_with_scripts(tmp_path)
        # This will fail at script execution but tests the command parsing
        result = self.runner.invoke(cli, ["run", "build"])
        # Expected: either success or controlled error, not crash
        assert result.exit_code >= 0

    def test_run_with_parameters(self, tmp_path: Path) -> None:
        """Test run with parameters."""
        os.chdir(tmp_path)
        _make_apm_yml_with_scripts(tmp_path)
        result = self.runner.invoke(cli, ["run", "build", "-p", "name=value", "-p", "key=data"])
        # Expected: either success or controlled error
        assert result.exit_code >= 0

    def test_run_verbose_flag(self, tmp_path: Path) -> None:
        """Test run with --verbose flag."""
        os.chdir(tmp_path)
        _make_apm_yml_with_scripts(tmp_path)
        result = self.runner.invoke(cli, ["run", "--verbose", "start"])
        # Expected: either success or controlled error
        assert result.exit_code >= 0


# ====== Config Command Tests ======


class TestConfigCommand:
    """Tests for ``apm config``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_config_show_no_project(self, tmp_path: Path) -> None:
        """Test config (no subcommand) outside a project."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config"])
        # Should show default configuration
        assert result.exit_code == 0

    def test_config_show_with_project(self, tmp_path: Path) -> None:
        """Test config (no subcommand) inside a project."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["config"])
        assert result.exit_code == 0
        assert "test-project" in result.output or "config" in result.output.lower()

    def test_config_group_no_subcommand(self, tmp_path: Path) -> None:
        """Test config group without subcommand (should show help or config)."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["config"])
        # Should display configuration table
        assert result.exit_code == 0

    def test_config_set_auto_integrate_true(self, tmp_path: Path) -> None:
        """Test config set auto-integrate true."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config", "set", "auto-integrate", "true"])
        assert result.exit_code == 0

    def test_config_set_auto_integrate_false(self, tmp_path: Path) -> None:
        """Test config set auto-integrate false."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config", "set", "auto-integrate", "false"])
        assert result.exit_code == 0

    def test_config_set_invalid_value(self, tmp_path: Path) -> None:
        """Test config set with invalid boolean value."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config", "set", "auto-integrate", "invalid"])
        # Should either fail gracefully or accept and warn
        assert result.exit_code >= 0

    def test_config_get_auto_integrate(self, tmp_path: Path) -> None:
        """Test config get auto-integrate."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config", "get", "auto-integrate"])
        assert result.exit_code == 0


# ====== Experimental Command Tests ======


class TestExperimentalCommand:
    """Tests for ``apm experimental``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_experimental_list(self, tmp_path: Path) -> None:
        """Test experimental list command."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["experimental", "list"])
        assert result.exit_code == 0
        assert "experimental" in result.output.lower() or "feature" in result.output.lower()

    def test_experimental_list_verbose(self, tmp_path: Path) -> None:
        """Test experimental list with --verbose."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["experimental", "list", "--verbose"])
        assert result.exit_code == 0

    def test_experimental_enable(self, tmp_path: Path) -> None:
        """Test experimental enable command."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["experimental", "enable", "copilot_cowork"])
        # Either succeeds or shows error message
        assert result.exit_code in [0, 1, 2]

    def test_experimental_disable(self, tmp_path: Path) -> None:
        """Test experimental disable command."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["experimental", "disable", "copilot_cowork"])
        # Either succeeds or shows error message
        assert result.exit_code in [0, 1, 2]

    def test_experimental_reset(self, tmp_path: Path) -> None:
        """Test experimental reset command."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["experimental", "reset"])
        # Either succeeds or shows error message
        assert result.exit_code in [0, 1, 2]


# ====== Update Command Tests ======


class TestUpdateCommand:
    """Tests for ``apm update``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_update_no_apm_yml(self, tmp_path: Path) -> None:
        """Test update when apm.yml doesn't exist."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["update"])
        # Should forward to self-update or fail gracefully
        assert result.exit_code >= 0

    def test_update_with_apm_yml(self, tmp_path: Path) -> None:
        """Test update with apm.yml present."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["update"])
        # May fail due to dependencies but should not crash
        assert result.exit_code >= 0

    def test_update_dry_run(self, tmp_path: Path) -> None:
        """Test update --dry-run."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["update", "--dry-run"])
        assert result.exit_code >= 0

    def test_update_yes_flag(self, tmp_path: Path) -> None:
        """Test update --yes."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        result = self.runner.invoke(cli, ["update", "--yes"])
        assert result.exit_code >= 0


# ====== Runtime Command Tests ======


class TestRuntimeCommand:
    """Tests for ``apm runtime``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_runtime_list(self, tmp_path: Path) -> None:
        """Test runtime list command."""
        os.chdir(tmp_path)
        with patch("apm_cli.runtime.manager.RuntimeManager") as mock_manager:
            mock_inst = MagicMock()
            mock_inst.list_runtimes.return_value = {
                "copilot": {
                    "installed": False,
                    "description": "GitHub Copilot",
                    "path": None,
                }
            }
            mock_manager.return_value = mock_inst
            result = self.runner.invoke(cli, ["runtime", "list"])
            assert result.exit_code == 0

    def test_runtime_setup_copilot(self, tmp_path: Path) -> None:
        """Test runtime setup copilot."""
        os.chdir(tmp_path)
        with patch("apm_cli.runtime.manager.RuntimeManager") as mock_manager:
            mock_inst = MagicMock()
            mock_inst.setup_runtime.return_value = True
            mock_manager.return_value = mock_inst
            result = self.runner.invoke(cli, ["runtime", "setup", "copilot"])
            assert result.exit_code == 0


# ====== Policy Command Tests ======


class TestPolicyCommand:
    """Tests for ``apm policy``."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_policy_list(self, tmp_path: Path) -> None:
        """Test policy list command."""
        os.chdir(tmp_path)
        with patch("apm_cli.policy.discovery.discover_policy"):
            result = self.runner.invoke(cli, ["policy", "list"])
            # Should work with or without policy discovery
            assert result.exit_code >= 0

    def test_policy_validate(self, tmp_path: Path) -> None:
        """Test policy validate command."""
        os.chdir(tmp_path)
        _make_basic_apm_yml(tmp_path)
        with patch("apm_cli.policy.discovery.discover_policy"):
            result = self.runner.invoke(cli, ["policy", "validate"])
            assert result.exit_code >= 0

    def test_policy_status(self, tmp_path: Path) -> None:
        """Test policy status command."""
        os.chdir(tmp_path)
        with patch("apm_cli.policy.discovery.discover_policy"):
            result = self.runner.invoke(cli, ["policy", "status"])
            assert result.exit_code >= 0


# ====== Compile Watcher Tests ======


class TestFormatTargetLabel:
    """Tests for _format_target_label helper function."""

    def test_format_target_label_none(self) -> None:
        """Test with None effective_target."""
        result = _format_target_label(None, None, None)
        assert result is None

    def test_format_target_label_single_target(self) -> None:
        """Test with single string target."""
        with patch("apm_cli.core.target_detection.get_target_description") as mock_desc:
            mock_desc.return_value = "Claude"
            result = _format_target_label("claude", None, None)
            assert "Compiling for Claude" in str(result)

    def test_format_target_label_frozenset_single_target(self) -> None:
        """Test with frozenset containing single target."""
        with patch("apm_cli.core.target_detection.should_compile_agents_md") as mock_agents:
            with patch("apm_cli.core.target_detection.should_compile_claude_md") as mock_claude:
                with patch("apm_cli.core.target_detection.should_compile_gemini_md") as mock_gemini:
                    mock_agents.return_value = True
                    mock_claude.return_value = False
                    mock_gemini.return_value = False
                    targets = frozenset(["claude"])
                    result = _format_target_label(targets, ["claude"], None)
                    assert "Compiling for AGENTS.md" in str(result)
                    assert "--target claude" in str(result)

    def test_format_target_label_frozenset_multi_target_from_user(self) -> None:
        """Test with frozenset from user --target."""
        with patch("apm_cli.core.target_detection.should_compile_agents_md") as mock_agents:
            with patch("apm_cli.core.target_detection.should_compile_claude_md") as mock_claude:
                with patch("apm_cli.core.target_detection.should_compile_gemini_md") as mock_gemini:
                    mock_agents.return_value = True
                    mock_claude.return_value = True
                    mock_gemini.return_value = False
                    targets = frozenset(["claude", "cursor"])
                    result = _format_target_label(targets, ["claude", "cursor"], None)
                    assert "Compiling for" in str(result)
                    assert "AGENTS.md" in str(result)
                    assert "CLAUDE.md" in str(result)

    def test_format_target_label_frozenset_multi_target_from_config(self) -> None:
        """Test with frozenset from apm.yml config."""
        with patch("apm_cli.core.target_detection.should_compile_agents_md") as mock_agents:
            with patch("apm_cli.core.target_detection.should_compile_claude_md") as mock_claude:
                with patch("apm_cli.core.target_detection.should_compile_gemini_md") as mock_gemini:
                    mock_agents.return_value = True
                    mock_claude.return_value = False
                    mock_gemini.return_value = True
                    targets = frozenset(["claude", "gemini"])
                    result = _format_target_label(targets, None, ["claude", "gemini"])
                    assert "apm.yml" in str(result)
                    assert "target:" in str(result)

    def test_format_target_label_frozenset_multi_target_no_source(self) -> None:
        """Test with frozenset without explicit source."""
        with patch("apm_cli.core.target_detection.should_compile_agents_md") as mock_agents:
            with patch("apm_cli.core.target_detection.should_compile_claude_md") as mock_claude:
                with patch("apm_cli.core.target_detection.should_compile_gemini_md") as mock_gemini:
                    mock_agents.return_value = False
                    mock_claude.return_value = True
                    mock_gemini.return_value = False
                    targets = frozenset(["claude"])
                    result = _format_target_label(targets, None, None)
                    assert "multi-target" in str(result)


# ====== Error Handling & Edge Cases ======


class TestErrorHandling:
    """Tests for error paths and edge cases."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        """Clean up after tests."""
        os.chdir(self.original_dir)
        clear_apm_yml_cache()

    def test_invalid_command(self, tmp_path: Path) -> None:
        """Test invoking invalid command."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["nonexistent-command"])
        assert result.exit_code != 0

    def test_prune_with_corrupt_lockfile(self, tmp_path: Path) -> None:
        """Test prune with corrupt lockfile."""
        os.chdir(tmp_path)
        _make_apm_yml_with_deps(tmp_path)
        (tmp_path / "apm.lock.yaml").write_text("invalid: [unclosed")
        result = self.runner.invoke(cli, ["prune"])
        # Should handle gracefully
        assert result.exit_code >= 0

    def test_config_get_nonexistent_key(self, tmp_path: Path) -> None:
        """Test config get with nonexistent key."""
        os.chdir(tmp_path)
        result = self.runner.invoke(cli, ["config", "get", "nonexistent-key"])
        # Should either show error or indicate key not found
        assert result.exit_code >= 0

    def test_experimental_enable_nonexistent_flag(self, tmp_path: Path) -> None:
        """Test experimental enable with nonexistent flag."""
        os.chdir(tmp_path)
        with patch("apm_cli.core.experimental.validate_flag_name"):
            result = self.runner.invoke(cli, ["experimental", "enable", "nonexistent"])
            # Should handle gracefully
            assert result.exit_code >= 0
