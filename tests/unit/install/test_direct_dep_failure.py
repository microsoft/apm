"""Tests for the fail-loud direct-dependency integration failure path.

When ``run_integration_template`` returns ``None`` for a direct dependency,
the integrate phase must set ``ctx.direct_dep_failed``, push an error to
the diagnostic collector, and the pipeline must raise
``DirectDependencyError`` so the CLI exits non-zero (#946).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.context import InstallContext
from apm_cli.install.errors import DirectDependencyError
from apm_cli.utils.diagnostics import DiagnosticCollector


def _make_ctx(tmp_path: Path, dep_keys: list[str]) -> InstallContext:
    """Build a minimal InstallContext with fake deps whose keys match *dep_keys*."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    apm_dir = project_root / ".apm"
    apm_dir.mkdir()
    modules = project_root / "apm_modules"
    modules.mkdir()

    deps = []
    for key in dep_keys:
        dep = MagicMock()
        dep.get_unique_key.return_value = key
        dep.alias = None
        dep.is_local = False
        dep.local_path = None
        dep.get_install_path.return_value = modules / key
        deps.append(dep)

    ctx = InstallContext(
        project_root=project_root,
        apm_dir=apm_dir,
    )
    ctx.apm_modules_dir = modules
    ctx.deps_to_install = list(deps)
    ctx.all_apm_deps = list(deps)
    ctx.diagnostics = DiagnosticCollector()
    ctx.logger = MagicMock()
    ctx.targets = []
    ctx.integrators = {}
    ctx.installed_packages = []
    return ctx


class TestDirectDepFailLoud:
    """Validate the fail-loud path when a direct dep fails integration."""

    @patch("apm_cli.install.phases.integrate.run_integration_template", return_value=None)
    @patch("apm_cli.install.phases.integrate.make_dependency_source")
    def test_integrate_sets_direct_dep_failed_on_none_deltas(
        self, _mock_source, _mock_template, tmp_path
    ):
        """When run_integration_template returns None for a direct dep,
        ctx.direct_dep_failed must be True."""
        from apm_cli.install.phases.integrate import run

        ctx = _make_ctx(tmp_path, ["owner/pkg"])
        run(ctx)

        assert ctx.direct_dep_failed is True

    @patch("apm_cli.install.phases.integrate.run_integration_template", return_value=None)
    @patch("apm_cli.install.phases.integrate.make_dependency_source")
    def test_integrate_pushes_error_to_diagnostics(
        self, _mock_source, _mock_template, tmp_path
    ):
        """The diagnostic collector must record the failure."""
        from apm_cli.install.phases.integrate import run

        ctx = _make_ctx(tmp_path, ["owner/pkg"])
        run(ctx)

        assert ctx.diagnostics.has_diagnostics
        assert ctx.diagnostics.error_count >= 1

    def test_pipeline_raises_direct_dependency_error(self, tmp_path):
        """run_install_pipeline must raise DirectDependencyError (not
        generic RuntimeError) when ctx.direct_dep_failed is set by the
        integrate phase."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = MagicMock()
        pkg.dependencies = {"apm": []}

        def _fake_integrate_run(ctx):
            """Simulate the integrate phase setting the failure flag."""
            ctx.direct_dep_failed = True

        with (
            patch("apm_cli.install.phases.integrate.run", side_effect=_fake_integrate_run),
            patch("apm_cli.install.phases.resolve.run"),
            patch("apm_cli.install.phases.targets.run"),
            patch("apm_cli.install.phases.download.run"),
        ):
            with pytest.raises(DirectDependencyError):
                run_install_pipeline(
                    apm_package=pkg,
                    verbose=False,
                    logger=MagicMock(),
                )

    @patch("apm_cli.install.phases.integrate.run_integration_template")
    @patch("apm_cli.install.phases.integrate.make_dependency_source")
    def test_transitive_failure_does_not_set_flag(
        self, _mock_source, mock_template, tmp_path
    ):
        """When a transitive (non-direct) dep returns None, direct_dep_failed
        must stay False -- only direct deps trigger the fail-loud path."""
        from apm_cli.install.phases.integrate import run

        mock_template.return_value = None

        # Build ctx with a transitive dep whose key differs from all_apm_deps
        ctx = _make_ctx(tmp_path, dep_keys=[])
        transitive = MagicMock()
        transitive.get_unique_key.return_value = "other/transitive"
        transitive.alias = None
        transitive.is_local = False
        transitive.local_path = None
        transitive.get_install_path.return_value = tmp_path / "apm_modules" / "other" / "transitive"
        ctx.deps_to_install = [transitive]
        # all_apm_deps is empty => transitive key not in direct_dep_keys

        run(ctx)

        assert ctx.direct_dep_failed is False
