"""Regression test for #1577: apm install -g must not write .gitignore.

Verifies that _update_gitignore_for_apm_modules is NOT called when
scope=InstallScope.USER (global install), and IS called for PROJECT scope.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.core.scope import InstallScope


def _make_pkg_with_dep() -> MagicMock:
    pkg = MagicMock()
    dep = MagicMock()
    dep.repo_url = "owner/repo"
    dep.host = "github.com"
    pkg.get_apm_dependencies.return_value = [dep]
    pkg.get_dev_apm_dependencies.return_value = []
    pkg.get_mcp_dependencies.return_value = []
    return pkg


def _common_patches():
    """Return a list of patch context managers shared by all tests."""
    return [
        patch("apm_cli.deps.lockfile.LockFile"),
        patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=None),
        patch("apm_cli.core.scope.get_deploy_root", return_value=MagicMock()),
        patch("apm_cli.core.scope.get_apm_dir", return_value=None),
        patch(
            "apm_cli.install.phases.local_content._project_has_root_primitives",
            return_value=False,
        ),
        patch("apm_cli.install.pipeline._run_phase"),
        patch("apm_cli.utils.install_tui.InstallTui"),
        patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None),
        patch(
            "apm_cli.integration.base_integrator.BaseIntegrator.normalize_managed_files",
            return_value=set(),
        ),
        patch("apm_cli.commands._helpers._update_gitignore_for_apm_modules"),
    ]


class TestGitignoreScopeGuard:
    """_update_gitignore_for_apm_modules must not run under InstallScope.USER."""

    def test_global_scope_does_not_call_update_gitignore(self) -> None:
        """Regression: apm install -g (USER scope) must never touch .gitignore."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = _make_pkg_with_dep()

        patches = _common_patches()
        with (
            patches[0] as mock_lf_cls,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5] as mock_run_phase,
            patches[6],
            patches[7],
            patches[8],
            patches[9] as mock_gitignore,
        ):
            mock_lf_cls.read.return_value = None

            def _resolve_sets_deps(name, phase, ctx):
                if name == "resolve":
                    ctx.deps_to_install = [MagicMock()]

            mock_run_phase.side_effect = _resolve_sets_deps

            run_install_pipeline(pkg, scope=InstallScope.USER)

        mock_gitignore.assert_not_called()

    def test_project_scope_calls_update_gitignore(self) -> None:
        """PROJECT scope (the default) must still update .gitignore."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = _make_pkg_with_dep()

        patches = _common_patches()
        with (
            patches[0] as mock_lf_cls,
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5] as mock_run_phase,
            patches[6],
            patches[7],
            patches[8],
            patches[9] as mock_gitignore,
        ):
            mock_lf_cls.read.return_value = None

            def _resolve_sets_deps(name, phase, ctx):
                if name == "resolve":
                    ctx.deps_to_install = [MagicMock()]

            mock_run_phase.side_effect = _resolve_sets_deps

            run_install_pipeline(pkg, scope=InstallScope.PROJECT)

        mock_gitignore.assert_called_once()
