"""Real resolver and materialization boundary coverage for issue #2815."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy
from apm_cli.install.context import InstallContext
from apm_cli.install.phases.resolve import _materialization, _resolve_dependencies
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.sources import LocalDependencySource
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.parametrize("boundary", ["resolve", "acquire"])
def test_user_scope_preserves_local_parent_anchor(tmp_path: Path, boundary: str) -> None:
    """Each production boundary must admit the same anchored transitive child."""
    factory = LocalPackageFactory(tmp_path / "packages")
    child = factory.create("child", targets=["cursor"])
    parent = factory.create("parent", dependencies=[{"path": "../child"}], targets=["cursor"])
    consumer = factory.create("consumer", dependencies=[{"path": parent.root.as_posix()}])
    package = APMPackage.from_apm_yml(consumer.manifest_path, source_path=consumer.root)
    modules = tmp_path / "user" / ".apm" / "apm_modules"
    modules.mkdir(parents=True)
    ctx = InstallContext(
        project_root=consumer.root,
        apm_dir=consumer.root,
        apm_package=package,
        # Isolate acquire from the resolver's admission decision.
        scope=InstallScope.PROJECT if boundary == "acquire" else InstallScope.USER,
        all_apm_deps=package.get_apm_dependencies(),
        apm_modules_dir=modules,
        ref_freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
        downloader=MagicMock(shared_clone_cache=None),
        diagnostics=DiagnosticCollector(),
    )
    staging = ResolutionStagingSession(modules)
    try:
        _resolve_dependencies(ctx, staging, _materialization.CachedMaterializationPathReader())
        child_ref = next(dep for dep in ctx.deps_to_install if dep.repo_url == "_local/child")
        if boundary == "resolve":
            assert not ctx.callback_failures
            assert child_ref.get_unique_key() in ctx.callback_downloaded
            node = ctx.dependency_graph.dependency_tree.get_node(child_ref.get_unique_key())
            assert node.package.source_path == child.root
        else:
            ctx.scope = InstallScope.USER
            source = LocalDependencySource(
                ctx, child_ref, child_ref.get_install_path(modules), child_ref.get_unique_key()
            )
            materialized = source.acquire()
            assert materialized is not None
            assert materialized.package_info.package.source_path == child.root
            assert ctx.installed_packages[0].resolved_by == "_local/parent"
        assert ctx.dep_base_dirs[child_ref.get_unique_key()] == parent.root
        assert child_ref.local_path == "../child"
        assert child_ref.declaring_parent == parent.root.as_posix()
        assert child_ref.get_install_path(modules).joinpath("apm.yml").read_bytes() == (
            child.manifest_path.read_bytes()
        )
        ctx.downloader.download_package.assert_not_called()
    finally:
        staging.rollback()
