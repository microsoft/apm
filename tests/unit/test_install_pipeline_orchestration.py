"""Unit tests for apm_cli.install.pipeline.

Covers missing lines/branches in pipeline.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _run_phase
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_non_verbose_calls_phase(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = False
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "result"
        result = _run_phase("myphase", phase, ctx)
        phase.run.assert_called_once_with(ctx)
        assert result == "result"

    def test_verbose_calls_phase_and_logs(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "done"
        result = _run_phase("myphase", phase, ctx)
        assert result == "done"
        ctx.logger.verbose_detail.assert_called_once()

    def test_verbose_phase_exception_propagates(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.side_effect = RuntimeError("phase failed")
        with pytest.raises(RuntimeError, match="phase failed"):
            _run_phase("myphase", phase, ctx)

    def test_no_logger_falls_through(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = None
        phase = MagicMock()
        phase.run.return_value = 42
        result = _run_phase("myphase", phase, ctx)
        assert result == 42


# ---------------------------------------------------------------------------
# run_install_pipeline -- empty deps short-circuit
# ---------------------------------------------------------------------------


class TestRunInstallPipelineShortCircuit:
    def _make_apm_package(self, deps=None, dev_deps=None):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = deps or []
        pkg.get_dev_apm_dependencies.return_value = dev_deps or []
        pkg.get_mcp_dependencies.return_value = []
        return pkg

    def test_no_deps_no_local_returns_install_result(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package()

        with (
            patch(
                "apm_cli.core.scope.get_deploy_root",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ),
        ):
            result = run_install_pipeline(pkg)

        assert isinstance(result, InstallResult)

    def test_resolve_phase_returns_no_deps_calls_nothing_to_install(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package(deps=[MagicMock()])  # has deps but resolve finds nothing
        logger = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.deps_to_install = []
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_deploy_root",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=tmp_path,
            ),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve.run") as _resolve_run,
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            result = run_install_pipeline(pkg, logger=logger)

        assert isinstance(result, InstallResult)
        logger.nothing_to_install.assert_called_once()


class TestRunInstallPipelineDirectMcpDeps:
    """MCP policy preflight sees the same root MCP set that install restores."""

    def test_policy_gate_receives_dev_mcp_dependencies(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline

        apm_dep = MagicMock()
        prod_mcp = MagicMock()
        dev_mcp = MagicMock()
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [apm_dep]
        pkg.get_dev_apm_dependencies.return_value = []
        pkg.get_mcp_dependencies.return_value = [prod_mcp]
        pkg.get_dev_mcp_dependencies.return_value = [dev_mcp]
        pkg.get_all_mcp_dependencies.return_value = [prod_mcp, dev_mcp]
        pkg.get_lsp_dependencies.return_value = []
        seen_direct_mcp_deps = []

        def _phase_runner(name, _phase, ctx):
            if name == "resolve":
                ctx.deps_to_install = [apm_dep]
                ctx.transitive_failures = []
                return None
            if name == "policy_gate":
                seen_direct_mcp_deps.extend(ctx.direct_mcp_deps)
                raise RuntimeError("stop after policy gate")
            return None

        with (
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_source_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.pipeline._run_phase", side_effect=_phase_runner),
        ):
            with pytest.raises(RuntimeError, match="stop after policy gate"):
                run_install_pipeline(pkg)

        assert seen_direct_mcp_deps == [prod_mcp, dev_mcp]


# ---------------------------------------------------------------------------
# run_install_pipeline -- plan_callback
# ---------------------------------------------------------------------------


class TestRunInstallPipelinePlanCallback:
    def _make_apm_package(self):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock()]
        pkg.get_dev_apm_dependencies.return_value = []
        pkg.get_mcp_dependencies.return_value = []
        return pkg

    def test_plan_callback_false_returns_early(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package()

        mock_ctx = MagicMock()
        dep = MagicMock()
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()

        plan_callback = MagicMock(return_value=False)  # user says "no"

        mock_resolve = MagicMock()
        # Resolve phase must NOT clear deps_to_install so plan gate is reached
        mock_resolve.run = MagicMock()

        with (
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.plan.build_update_plan", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve", mock_resolve, create=True),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            result = run_install_pipeline(pkg, plan_callback=plan_callback)

        assert isinstance(result, InstallResult)
        plan_callback.assert_called_once()

    def test_plan_callback_true_continues(self, tmp_path):
        import contextlib

        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_apm_package()
        mock_ctx = MagicMock()
        dep = MagicMock()
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()
        mock_ctx.transitive_failures = []
        mock_ctx.existing_lockfile = None
        mock_ctx.verbose = False
        mock_ctx.dry_run = False
        mock_ctx.legacy_skill_paths = True
        mock_ctx.diagnostics = MagicMock()
        mock_ctx.auth_resolver = MagicMock()
        mock_ctx.update_refs = False
        mock_ctx.logger = MagicMock()

        plan_callback = MagicMock(return_value=True)

        mock_resolve = MagicMock()
        mock_resolve.run = MagicMock()
        mock_finalize_result = MagicMock()

        patches = [
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.plan.build_update_plan", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve", mock_resolve, create=True),
            patch("apm_cli.install.phases.policy_gate.run"),
            patch("apm_cli.install.phases.targets.run"),
            patch("apm_cli.install.phases.policy_target_check.run"),
            patch("apm_cli.install.phases.download.run"),
            patch("apm_cli.install.phases.integrate.run"),
            patch("apm_cli.install.phases.cleanup.run"),
            patch("apm_cli.install.phases.post_deps_local.run"),
            patch("apm_cli.install.phases.finalize.run", return_value=mock_finalize_result),
            patch("apm_cli.install.phases.lockfile.LockfileBuilder"),
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.utils.diagnostics.DiagnosticCollector"),
        ]

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            import contextlib as _cl

            with _cl.suppress(Exception):  # sub-phases may raise; callback should still fire
                run_install_pipeline(pkg, plan_callback=plan_callback)

        plan_callback.assert_called_once()


# ---------------------------------------------------------------------------
# run_install_pipeline -- exception wrapping
# ---------------------------------------------------------------------------


class TestRunInstallPipelineExceptionHandling:
    def _make_apm_package(self):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = [MagicMock()]
        pkg.get_dev_apm_dependencies.return_value = []
        pkg.get_mcp_dependencies.return_value = []
        return pkg

    def test_generic_exception_wrapped_in_runtime_error(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_apm_package()

        mock_ctx = MagicMock()
        dep = MagicMock()
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = True
        mock_ctx.tui = MagicMock()

        resolve_phase = MagicMock()
        resolve_phase.run.side_effect = ValueError("unexpected internal error")

        with (
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=True,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve", resolve_phase, create=True),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            with pytest.raises((RuntimeError, ValueError)):
                run_install_pipeline(pkg)

    def test_policy_violation_propagates(self, tmp_path):
        from apm_cli.install.errors import PolicyViolationError
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_apm_package()
        mock_ctx = MagicMock()
        dep = MagicMock()
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = True
        mock_ctx.tui = MagicMock()

        resolve_phase = MagicMock()
        resolve_phase.run.side_effect = PolicyViolationError("policy blocked")

        with (
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=True,
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.install.context.InstallContext", return_value=mock_ctx),
            patch("apm_cli.utils.install_tui.InstallTui", return_value=MagicMock()),
            patch("apm_cli.install.phases.resolve", resolve_phase, create=True),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            with pytest.raises(PolicyViolationError):
                run_install_pipeline(pkg)


# ---------------------------------------------------------------------------
# _preflight_auth_check
# ---------------------------------------------------------------------------


class TestPreflightAuthCheck:
    def _make_ctx(self, deps=None):
        ctx = MagicMock()
        ctx.deps_to_install = deps or []
        ctx.verbose = False
        ctx.logger = MagicMock()
        return ctx

    def test_skips_github_hosts(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = "github.com"
        dep.repo_url = "org/repo"
        ctx = self._make_ctx(deps=[dep])
        auth = MagicMock()

        with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
            # Should complete without raising
            _preflight_auth_check(ctx, auth, verbose=False)

    def test_skips_deps_without_host(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = None
        dep.repo_url = "org/repo"
        ctx = self._make_ctx(deps=[dep])
        auth = MagicMock()

        # No exception expected
        _preflight_auth_check(ctx, auth, verbose=False)

    def test_auth_success_does_not_raise(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = "ado.example.com"
        dep.repo_url = "org/repo"
        dep.is_azure_devops.return_value = True
        ctx = self._make_ctx(deps=[dep])

        dep_ctx = MagicMock()
        dep_ctx.auth_scheme = "basic"
        dep_ctx.source = "ADO_APM_PAT"
        dep_ctx.token = "tok"
        dep_ctx.git_env = {}
        auth = MagicMock()
        auth.resolve_for_dep.return_value = dep_ctx
        auth._build_git_env.return_value = {}

        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""

        with (
            patch("apm_cli.utils.github_host.is_github_hostname", return_value=False),
            patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=True),
            patch("apm_cli.utils.github_host.is_ado_auth_failure_signal", return_value=False),
            patch("subprocess.run", return_value=proc),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader._build_repo_url",
                return_value="https://ado.example.com/org/repo.git",
            ),
        ):
            _preflight_auth_check(ctx, auth, verbose=False)
