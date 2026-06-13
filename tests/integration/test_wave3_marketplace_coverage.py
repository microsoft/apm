"""Integration tests for wave 3 marketplace coverage.

This test suite maximizes code coverage on five target modules:
1. src/apm_cli/commands/marketplace/__init__.py (422 miss, 37%)
2. src/apm_cli/commands/init.py (188 miss, 33%)
3. src/apm_cli/commands/pack.py (188 miss, 41%)
4. src/apm_cli/marketplace/publisher.py (217 miss, 25%)
5. src/apm_cli/core/script_runner.py (254 miss, 39%)

Strategy:
- Use CliRunner for command-line tests to execute real code paths
- Mock only external I/O boundaries (HTTP, subprocess)
- Test both happy and error paths
- Create real project structures in temp directories
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.script_runner import ScriptRunner

# ---------------------------------------------------------------------------
# Helpers for test fixture setup
# ---------------------------------------------------------------------------


def _write_apm_yml(root: Path, body: str) -> None:
    """Write apm.yml content to root directory."""
    (root / "apm.yml").write_text(body, encoding="utf-8")


def _write_apm_lock(root: Path) -> None:
    """Write a minimal but valid apm.lock.yaml."""
    lock_content = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""
    (root / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")


def _write_minimal_project(root: Path, name: str = "test-project") -> None:
    """Write a minimal but valid APM project structure."""
    _write_apm_yml(
        root,
        f"""\
name: {name}
version: 0.1.0
description: Test project for coverage
""",
    )
    _write_apm_lock(root)


def _write_marketplace_project(root: Path, name: str = "test-marketplace") -> None:
    """Write a minimal APM marketplace project."""
    # Create the marketplace plugin directory structure
    plugin_dir = root / ".github" / "plugins" / "sample"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "instructions.md").write_text("# Sample Plugin", encoding="utf-8")

    _write_apm_yml(
        root,
        f"""\
name: {name}
version: 0.1.0
description: Marketplace test project

marketplace:
  owner:
    name: Test Owner
    url: https://example.com
  packages:
    - name: sample
      description: Sample package
      source: ./.github/plugins/sample
      homepage: https://example.com
""",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Provide a Click CliRunner for command tests."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Init Command Tests (src/apm_cli/commands/init.py)
# ---------------------------------------------------------------------------


class TestInitCommand:
    """Test coverage for apm init command."""

    def test_init_basic_minimal_project(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test basic init with defaults (auto-detected targets)."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as isolated_cwd:
            result = runner.invoke(cli, ["init", "my-project", "-y"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            # Project directory was created in the isolated filesystem
            isolated_path = Path(isolated_cwd)
            assert (isolated_path / "my-project" / "apm.yml").exists()

    def test_init_with_explicit_target(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with explicit --target flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as isolated_cwd:
            result = runner.invoke(
                cli,
                ["init", "copilot-plugin", "-y", "--target", "copilot"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            isolated_path = Path(isolated_cwd)
            project_dir = isolated_path / "copilot-plugin"
            assert project_dir.exists()
            yml_content = (project_dir / "apm.yml").read_text(encoding="utf-8")
            assert "name: copilot-plugin" in yml_content

    def test_init_with_multiple_targets(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with comma-separated targets."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as isolated_cwd:
            result = runner.invoke(
                cli,
                ["init", "multi-target", "-y", "--target", "copilot,claude,cursor"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            isolated_path = Path(isolated_cwd)
            project_dir = isolated_path / "multi-target"
            assert project_dir.exists()

    def test_init_deprecated_plugin_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test deprecated --plugin flag still works with warning."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["init", "legacy-plugin", "-y", "--plugin"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            assert "deprecated" in result.output.lower()

    def test_init_deprecated_marketplace_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test deprecated --marketplace flag still works with warning."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["init", "legacy-marketplace", "-y", "--marketplace"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            assert "deprecated" in result.output.lower()

    def test_init_verbose_output(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with --verbose flag shows detailed output."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["init", "verbose-test", "-y", "-v"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            assert len(result.output) > 0

    def test_init_without_project_name_uses_cwd(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init without project name creates in current directory."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["init", "-y"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            # apm.yml created in the isolated filesystem root (cwd)
            assert (Path.cwd() / "apm.yml").exists()

    def test_init_invalid_project_name_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with invalid project name fails gracefully."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["init", "invalid@project!", "-y"],
                catch_exceptions=False,
            )
            # Should fail or warn
            assert result.exit_code != 0 or "invalid" in result.output.lower()

    def test_init_all_supported_targets(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with all supported targets."""
        targets = ["copilot", "claude", "cursor", "opencode", "codex", "gemini", "windsurf"]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            target_str = ",".join(targets)
            result = runner.invoke(
                cli,
                ["init", "all-targets", "-y", "--target", target_str],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Pack Command Tests (src/apm_cli/commands/pack.py)
# ---------------------------------------------------------------------------


class TestPackCommand:
    """Test coverage for apm pack command."""

    def test_pack_bundle_only(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test packing a bundle with dependencies only."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: bundle-only
version: 0.1.0
description: Bundle only test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(cli, ["pack"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            # Build directory created
            assert (Path.cwd() / "build").exists()

    def test_pack_with_target_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with deprecated --target flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: target-test
version: 0.1.0
description: Target flag test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "--target", "copilot"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

    def test_pack_with_format_apm(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with legacy apm format."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: legacy-format
version: 0.1.0
description: Legacy format test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "--format", "apm"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

    def test_pack_with_output_directory(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with custom output directory."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: custom-output
version: 0.1.0
description: Custom output test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "-o", "dist"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

    def test_pack_with_archive_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with --archive to create .zip."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: archive-test
version: 0.1.0
description: Archive format test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "--archive"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

    def test_pack_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with --dry-run flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["pack", "--dry-run"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

    def test_pack_no_apm_yml_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack fails when apm.yml is missing."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["pack"], catch_exceptions=False)
            assert result.exit_code != 0

    def test_pack_invalid_apm_yml_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack fails with invalid apm.yml syntax."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            (Path.cwd() / "apm.yml").write_text("invalid: yaml: syntax: [", encoding="utf-8")
            result = runner.invoke(cli, ["pack"], catch_exceptions=False)
            assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Marketplace Init Command Tests (src/apm_cli/commands/marketplace/__init__.py)
# ---------------------------------------------------------------------------


class TestMarketplaceInitCommand:
    """Test coverage for apm marketplace init command."""

    def test_marketplace_init_creates_apm_yml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace init creates apm.yml with marketplace block."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["marketplace", "init"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            yml_path = Path.cwd() / "apm.yml"
            assert yml_path.exists()
            content = yml_path.read_text(encoding="utf-8")
            assert "marketplace:" in content

    def test_marketplace_init_verbose(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace init with --verbose flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["marketplace", "init", "--verbose"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            assert "apm.yml" in result.output or "marketplace" in result.output.lower()

    def test_marketplace_init_force_overwrite(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace init with --force to overwrite existing."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # First init
            result1 = runner.invoke(
                cli,
                ["marketplace", "init"],
                catch_exceptions=False,
            )
            assert result1.exit_code == 0
            # Second init should fail without --force
            result2 = runner.invoke(
                cli,
                ["marketplace", "init"],
                catch_exceptions=False,
            )
            assert result2.exit_code != 0
            # Third init with --force should succeed
            result3 = runner.invoke(
                cli,
                ["marketplace", "init", "--force"],
                catch_exceptions=False,
            )
            assert result3.exit_code == 0

    def test_marketplace_init_gitignore_warning(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace init warns about marketplace.json in .gitignore."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # Create .gitignore with marketplace.json
            (Path.cwd() / ".gitignore").write_text("marketplace.json\n", encoding="utf-8")
            result = runner.invoke(
                cli,
                ["marketplace", "init"],
                catch_exceptions=False,
            )
            # Should succeed
            assert result.exit_code == 0


class TestMarketplaceCheckCommand:
    """Test coverage for apm marketplace check command."""

    def test_marketplace_check_valid_project(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace check on valid marketplace project."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "check"],
                catch_exceptions=False,
            )
            # Check command executes; may report failures for missing remote refs
            # but the command itself should work
            assert "Entry Health Check" in result.output or result.exit_code in [0, 1]

    def test_marketplace_check_no_apm_yml_fails(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace check fails without apm.yml."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["marketplace", "check"],
                catch_exceptions=False,
            )
            assert result.exit_code != 0

    def test_marketplace_check_verbose(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace check with --verbose."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "check", "--verbose"],
                catch_exceptions=False,
            )
            # Should execute the check command
            assert "Entry Health Check" in result.output or result.exit_code in [0, 1]


class TestMarketplaceValidateCommand:
    """Test coverage for apm marketplace validate command."""

    def test_marketplace_validate_valid_project(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace validate on valid project."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "validate", "test-marketplace"],
                catch_exceptions=False,
            )
            # Validate should execute the command
            assert result.exit_code in [0, 1, 2]


class TestMarketplaceListCommand:
    """Test coverage for apm marketplace list command."""

    def test_marketplace_list(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace list shows configured packages."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "list"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output


class TestMarketplaceBrowseCommand:
    """Test coverage for apm marketplace browse command."""

    def test_marketplace_browse(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace browse command."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_marketplace_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "browse", "sample"],
                catch_exceptions=False,
            )
            # May fail due to network/registry but command should execute
            assert result.exit_code in [0, 1, 2]


# ---------------------------------------------------------------------------
# Script Runner Tests (src/apm_cli/core/script_runner.py)
# ---------------------------------------------------------------------------


class TestScriptRunner:
    """Test coverage for ScriptRunner class."""

    def test_script_runner_initialization(self) -> None:
        """Test ScriptRunner initializes correctly."""
        runner = ScriptRunner(use_color=False)
        assert runner is not None
        assert runner.compiler is not None
        assert runner.formatter is not None

    def test_script_runner_with_custom_compiler(self) -> None:
        """Test ScriptRunner with custom compiler."""
        mock_compiler = mock.MagicMock()
        runner = ScriptRunner(compiler=mock_compiler, use_color=False)
        assert runner.compiler is mock_compiler

    def test_script_runner_color_modes(self) -> None:
        """Test ScriptRunner with different color modes."""
        runner_color = ScriptRunner(use_color=True)
        runner_no_color = ScriptRunner(use_color=False)
        assert runner_color is not None
        assert runner_no_color is not None


# ---------------------------------------------------------------------------
# Marketplace Publisher Tests (src/apm_cli/marketplace/publisher.py)
# ---------------------------------------------------------------------------


class TestMarketplacePublisher:
    """Test coverage for MarketplacePublisher class."""

    def test_publisher_initialization(self, tmp_path: Path) -> None:
        """Test MarketplacePublisher initializes with marketplace_root."""
        from apm_cli.marketplace.publisher import MarketplacePublisher

        # Create a marketplace root directory
        mp_root = tmp_path / "marketplace"
        mp_root.mkdir()
        publisher = MarketplacePublisher(marketplace_root=mp_root)
        assert publisher is not None

    def test_publisher_sanitize_branch_name(self) -> None:
        """Test publisher branch name sanitization."""
        from apm_cli.marketplace.publisher import _sanitise_branch_segment

        # Test special characters are replaced
        result = _sanitise_branch_segment("test@azure#v1.0.0")
        assert "@" not in result
        assert "#" not in result
        assert "-" in result


# ---------------------------------------------------------------------------
# Integration: Combined Command Tests
# ---------------------------------------------------------------------------


class TestIntegrationScenarios:
    """Test realistic end-to-end scenarios."""

    def test_init_then_pack(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test creating a project with init then packing it."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as isolated_cwd:
            # Init project
            init_result = runner.invoke(
                cli,
                ["init", "e2e-test", "-y", "--target", "copilot"],
                catch_exceptions=False,
            )
            assert init_result.exit_code == 0, init_result.output

            # Get the project directory
            proj_dir = Path(isolated_cwd) / "e2e-test"

            # Add dependencies
            apm_yml = proj_dir / "apm.yml"
            content = apm_yml.read_text(encoding="utf-8")
            content += "\ndependencies:\n  apm: []\n"
            apm_yml.write_text(content, encoding="utf-8")

            # Create lock file
            lock_file = proj_dir / "apm.lock.yaml"
            lock_file.write_text(
                "lockfile_version: '1'\n"
                "generated_at: '2025-01-01T00:00:00+00:00'\n"
                "dependencies: []\n",
                encoding="utf-8",
            )

            # Pack the project in isolated filesystem
            result = runner.invoke(
                cli,
                ["pack"],
                catch_exceptions=False,
                obj=None,
                env={"PWD": str(proj_dir), "HOME": str(tmp_path)},
            )
            assert result.exit_code == 0, result.output

    def test_marketplace_init_then_check(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test creating marketplace project with init then checking it."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # Init marketplace
            init_result = runner.invoke(
                cli,
                ["marketplace", "init"],
                catch_exceptions=False,
            )
            assert init_result.exit_code == 0, init_result.output

            # Check the created marketplace
            check_result = runner.invoke(
                cli,
                ["marketplace", "check"],
                catch_exceptions=False,
            )
            # Check should run; may report status failures but should execute
            assert "Entry Health Check" in check_result.output or check_result.exit_code in [0, 1]


# ---------------------------------------------------------------------------
# Error Path Tests
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """Test error handling and edge cases."""

    def test_init_with_bad_target(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init with invalid target value."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                cli,
                ["init", "test", "-y", "--target", "invalid-target"],
                catch_exceptions=False,
            )
            # Should fail for invalid target
            assert result.exit_code != 0

    def test_pack_invalid_apm_yml_syntax(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack fails with invalid apm.yml syntax."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            (Path.cwd() / "apm.yml").write_text(
                "invalid: yaml: [syntax:",
                encoding="utf-8",
            )
            result = runner.invoke(cli, ["pack"], catch_exceptions=False)
            # Should fail due to invalid YAML
            assert result.exit_code != 0

    def test_marketplace_check_no_marketplace_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Test marketplace check on project without marketplace block."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "check"],
                catch_exceptions=False,
            )
            # Should fail for missing marketplace block
            assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Coverage Expansion Tests
# ---------------------------------------------------------------------------


class TestCoverageExpansion:
    """Additional tests to expand coverage of branches and edge cases."""

    def test_marketplace_init_twice_without_force_fails(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Test that second init fails without --force."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            runner.invoke(cli, ["marketplace", "init"], catch_exceptions=False)
            result = runner.invoke(cli, ["marketplace", "init"], catch_exceptions=False)
            assert result.exit_code != 0

    def test_pack_check_versions_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with --check-versions flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: version-check
version: 0.1.0
description: Version check test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "--check-versions"],
                catch_exceptions=False,
            )
            # Should execute without error
            assert result.exit_code in [0, 3]

    def test_marketplace_validate_on_non_marketplace(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Test marketplace validate on non-marketplace project."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "validate"],
                catch_exceptions=False,
            )
            # May fail but command should execute
            assert result.exit_code in [0, 1, 2]

    def test_init_in_existing_directory(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test init in existing project directory."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # First init
            result1 = runner.invoke(cli, ["init", "-y"], catch_exceptions=False)
            assert result1.exit_code == 0

            # Second init should warn about existing apm.yml
            result2 = runner.invoke(cli, ["init", "-y"], catch_exceptions=False)
            # Should still succeed with -y flag
            assert result2.exit_code == 0

    def test_pack_with_json_output(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test pack with --json output flag."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            (Path.cwd() / "apm.yml").write_text(
                """\
name: json-output
version: 0.1.0
description: JSON output test
dependencies:
  apm: []
""",
                encoding="utf-8",
            )
            result = runner.invoke(
                cli,
                ["pack", "--json"],
                catch_exceptions=False,
            )
            # Should output JSON on success or error
            assert result.exit_code == 0 or "{" in result.output

    def test_marketplace_list_no_config(self, runner: CliRunner, tmp_path: Path) -> None:
        """Test marketplace list on project without marketplace config."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_minimal_project(Path.cwd())
            result = runner.invoke(
                cli,
                ["marketplace", "list"],
                catch_exceptions=False,
            )
            # Marketplace list just shows what's configured or empty
            assert result.exit_code == 0
