"""Cached materialization restores canonical Git source identity."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.context import InstallContext
from apm_cli.install.sources import CachedDependencySource
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.parametrize("fetched_this_run", [False, True], ids=["cached", "fresh"])
def test_cached_acquisition_restores_original_git_source(
    tmp_path: Path, fetched_this_run: bool
) -> None:
    """Authored metadata cannot replace the Git origin used for reconstruction."""
    package = LocalPackageFactory(tmp_path).create("parent")
    dump_yaml(
        {"name": "parent", "version": "1.0.0", "source": "_local/forged"},
        package.manifest_path,
    )
    ref = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/team/parent", "ref": "v1"}
    )
    graph = MagicMock()
    graph.dependency_tree.get_node.return_value = None
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        scope=InstallScope.PROJECT,
        dependency_graph=graph,
        diagnostics=DiagnosticCollector(),
        targets=[KNOWN_TARGETS["cursor"]],
    )
    source = CachedDependencySource(
        ctx,
        ref,
        package.root,
        ref.get_unique_key(),
        resolved_ref=None,
        dep_locked_chk=None,
        fetched_this_run=fetched_this_run,
    )

    materialized = source.acquire()

    assert materialized is not None
    assert materialized.package_info is not None
    restored = urlparse(materialized.package_info.package.source)
    assert restored.scheme == "https"
    assert restored.hostname == "gitlab.example.invalid"
    assert restored == urlparse(ref.to_github_url())
    assert ctx.installed_packages[0].dep_ref is ref
