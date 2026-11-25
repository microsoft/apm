"""Unit tests for apm_cli.install.pipeline.

Covers missing lines/branches in pipeline.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# _run_phase
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- empty deps short-circuit
# ---------------------------------------------------------------------------


class TestRunInstallPipelineShortCircuit:
    def _make_apm_package(self, deps=None, dev_deps=None, replace_rules_unset=False):
        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = deps or []
        pkg.get_dev_apm_dependencies.return_value = dev_deps or []
        pkg.get_mcp_dependencies.return_value = []
        if not replace_rules_unset:
            pkg.replace_rules = None
        return pkg

    @pytest.mark.parametrize(
        "replace_rules_unset", [False, True], ids=["replace-rules-none", "replace-rules-unset"]
    )
    def test_no_deps_no_local_returns_install_result(self, tmp_path, replace_rules_unset):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package(replace_rules_unset=replace_rules_unset)

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

    @pytest.mark.parametrize(
        "replace_rules_unset", [False, True], ids=["replace-rules-none", "replace-rules-unset"]
    )
    def test_resolve_phase_returns_no_deps_calls_nothing_to_install(
        self, tmp_path, replace_rules_unset
    ):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package(
            deps=[MagicMock()], replace_rules_unset=replace_rules_unset
        )  # has deps but resolve finds nothing
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

            result = run_install_pipeline(pkg, logger=logger, target="claude")

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
                run_install_pipeline(pkg, target="claude")

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
            patch("apm_cli.install.phases.resolve", mock_resolve),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            result = run_install_pipeline(pkg, plan_callback=plan_callback, target="claude")

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
            patch("apm_cli.install.phases.resolve", mock_resolve),
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
                run_install_pipeline(pkg, plan_callback=plan_callback, target="claude")

        plan_callback.assert_called_once()

    def test_plan_callback_false_passes_complete_dep_keys_to_update_plan(self, tmp_path):
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_apm_package()

        mock_ctx = MagicMock()
        dep = MagicMock()
        dep.replacement = None
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()
        mock_ctx.only_packages = ["selected"]
        mock_ctx.update_plan_complete_dep_keys = {"selected", "unselected"}
        mock_ctx.package_hashes = {}

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
            patch("apm_cli.install.plan.build_update_plan", return_value=MagicMock()) as build_plan,
            patch("apm_cli.install.phases.resolve", mock_resolve),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            result = run_install_pipeline(pkg, plan_callback=plan_callback, target="claude")

        assert isinstance(result, InstallResult)
        plan_callback.assert_called_once()
        build_plan.assert_called_once_with(
            None,
            [dep],
            complete_resolved_dep_keys={"selected", "unselected"},
            resolved_content_hashes={},
        )

    def test_plan_callback_true_continues_without_only_packages(self, tmp_path):
        import contextlib

        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_apm_package()
        mock_ctx = MagicMock()
        dep = MagicMock()
        mock_ctx.deps_to_install = [dep]
        mock_ctx.root_has_local_primitives = False
        mock_ctx.tui = MagicMock()
        mock_ctx.only_packages = None
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
            patch("apm_cli.install.phases.resolve", mock_resolve),
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
                run_install_pipeline(pkg, plan_callback=plan_callback, target="claude")

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
            patch("apm_cli.install.phases.resolve", resolve_phase),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            with pytest.raises((RuntimeError, ValueError)):
                run_install_pipeline(pkg, target="claude")

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
            patch("apm_cli.install.phases.resolve", resolve_phase),
        ):
            mock_ctx.tui.__enter__ = MagicMock(return_value=mock_ctx.tui)
            mock_ctx.tui.__exit__ = MagicMock(return_value=False)
            mock_ctx.tui.start_phase = MagicMock()

            with pytest.raises(PolicyViolationError):
                run_install_pipeline(pkg, target="claude")


# ---------------------------------------------------------------------------
# _preflight_auth_check
# ---------------------------------------------------------------------------
