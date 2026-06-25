"""Unit tests for ``apm mcp export`` command.

Covers:
* unknown --runtime exits non-zero with [x] message
* missing apm.yml exits non-zero with [x] message
* apm.yml with no mcp deps exits 0 with [i] message
* valid runtime calls _apply_mcp_configs (the shared config-generation path)
* multiple --runtime flags each get processed
* runtime excluded by targets: whitelist is skipped with [!] warning
* no dependency resolution (lockfile unchanged, no resolver invoked)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.commands.mcp import mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APM_YML_WITH_MCP = """\
name: test-project
version: 0.1.0
dependencies:
  mcp:
    - command: python -m my_server
      name: my-server
      registry: false
"""

APM_YML_NO_MCP = """\
name: test-project
version: 0.1.0
dependencies:
  apm: []
"""

APM_YML_WITH_TARGETS = """\
name: test-project
version: 0.1.0
targets:
  - copilot
dependencies:
  mcp:
    - command: python -m my_server
      name: my-server
      registry: false
"""


def _make_self_defined_dep(name: str = "my-server") -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = False
    dep.is_self_defined = True
    dep.transport = "stdio"
    dep.command = "python"
    dep.args = ["-m", "my_server"]
    dep.env = {}
    dep.tools = None
    dep.headers = None
    dep.url = None
    dep.extra = None
    return dep


# ---------------------------------------------------------------------------
# Unknown runtime
# ---------------------------------------------------------------------------


class TestExportUnknownRuntime:
    def test_unknown_runtime_exits_nonzero(self):
        runner = CliRunner()
        result = runner.invoke(mcp, ["export", "--runtime", "not-a-real-runtime"])
        assert result.exit_code != 0

    def test_unknown_runtime_prints_error_symbol(self):
        runner = CliRunner()
        result = runner.invoke(mcp, ["export", "--runtime", "not-a-real-runtime"])
        assert "[x]" in result.output

    def test_unknown_runtime_mentions_name(self):
        runner = CliRunner()
        result = runner.invoke(mcp, ["export", "--runtime", "not-a-real-runtime"])
        assert "not-a-real-runtime" in result.output


# ---------------------------------------------------------------------------
# Missing apm.yml
# ---------------------------------------------------------------------------


class TestExportMissingApmYml:
    def test_no_apm_yml_exits_nonzero(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])
        assert result.exit_code != 0

    def test_no_apm_yml_prints_error_symbol(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])
        assert "[x]" in result.output


# ---------------------------------------------------------------------------
# No MCP deps in apm.yml
# ---------------------------------------------------------------------------


class TestExportNoMcpDeps:
    def test_no_mcp_deps_exits_zero(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text(APM_YML_NO_MCP, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])
        assert result.exit_code == 0

    def test_no_mcp_deps_prints_info_symbol(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text(APM_YML_NO_MCP, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])
        assert "[i]" in result.output


# ---------------------------------------------------------------------------
# Config generation is invoked via _apply_mcp_configs
# ---------------------------------------------------------------------------


class TestExportCallsApplyMcpConfigs:
    def test_single_runtime_calls_apply_mcp_configs(self, tmp_path):
        """export --runtime vscode must call _apply_mcp_configs once."""
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(1, set()),
            ) as mock_apply,
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_MCP, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])

        assert result.exit_code == 0, result.output
        mock_apply.assert_called_once()
        # target_runtimes kwarg must include "vscode"
        call_kwargs = mock_apply.call_args.kwargs
        target_runtimes = call_kwargs["target_runtimes"]
        assert "vscode" in target_runtimes

    def test_single_runtime_exits_zero_on_success(self, tmp_path):
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(1, set()),
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_MCP, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Multiple --runtime flags
# ---------------------------------------------------------------------------


class TestExportMultipleRuntimes:
    def test_multiple_runtimes_all_passed_to_apply(self, tmp_path):
        """--runtime vscode --runtime copilot both appear in target_runtimes."""
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(2, set()),
            ) as mock_apply,
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_MCP, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode", "--runtime", "copilot"])

        assert result.exit_code == 0, result.output
        mock_apply.assert_called_once()
        target_runtimes = mock_apply.call_args.kwargs["target_runtimes"]
        assert "vscode" in target_runtimes
        assert "copilot" in target_runtimes


# ---------------------------------------------------------------------------
# targets: whitelist respected
# ---------------------------------------------------------------------------


class TestExportTargetsWhitelist:
    def test_excluded_runtime_skipped_with_warning(self, tmp_path):
        """Runtime excluded by targets: whitelist shows [!] warning."""
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(0, set()),
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                # Simulate whitelist excluding vscode (only copilot is active)
                side_effect=lambda rts, **kw: [r for r in rts if r != "vscode"],
            ),
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_TARGETS, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])

        assert "[!]" in result.output

    def test_all_runtimes_excluded_exits_nonzero(self, tmp_path):
        """If all requested runtimes are excluded, exit non-zero."""
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(0, set()),
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                # Exclude all requested runtimes
                side_effect=lambda rts, **kw: [],
            ),
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_TARGETS, encoding="utf-8")
            result = runner.invoke(mcp, ["export", "--runtime", "vscode"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# No dependency resolution and no lockfile mutation
# ---------------------------------------------------------------------------


class TestExportNoResolution:
    def test_lockfile_not_mutated(self, tmp_path):
        """apm.lock.yaml must not be written during export."""
        runner = CliRunner()
        dep = _make_self_defined_dep()
        lock_content = "generated_at: '2024-01-01T00:00:00+00:00'\nschema_version: 1\n"

        with (
            runner.isolated_filesystem(temp_dir=tmp_path) as td,
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(1, set()),
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator.update_lockfile"
            ) as mock_update_lf,
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            lock_path = Path(td) / "apm.lock.yaml"
            Path("apm.yml").write_text(APM_YML_WITH_MCP, encoding="utf-8")
            lock_path.write_text(lock_content, encoding="utf-8")
            runner.invoke(mcp, ["export", "--runtime", "vscode"])

            # update_lockfile must never be called from export
            mock_update_lf.assert_not_called()

        # Lock file on disk must be unchanged
        assert lock_path.read_text(encoding="utf-8") == lock_content

    def test_no_apt_package_resolver_invoked(self, tmp_path):
        """APM package dependency resolver must not be invoked during export."""
        runner = CliRunner()
        dep = _make_self_defined_dep()

        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                autospec=True,
            ) as mock_pkg_cls,
            patch(
                "apm_cli.integration.mcp_integrator_install._apply_mcp_configs",
                return_value=(1, set()),
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            # Assert that the APM dependency resolution pipeline is never called
            patch("apm_cli.commands.install._resolve_package_references") as mock_resolver,
        ):
            mock_pkg = MagicMock()
            mock_pkg.get_all_mcp_dependencies.return_value = [dep]
            mock_pkg_cls.return_value = mock_pkg

            Path("apm.yml").write_text(APM_YML_WITH_MCP, encoding="utf-8")
            runner.invoke(mcp, ["export", "--runtime", "vscode"])

            mock_resolver.assert_not_called()
