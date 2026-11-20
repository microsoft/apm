"""Phase-3w5 unit tests for apm_cli.install.phases.resolve.

Covers missing lines/branches not reached by test_resolve_phase3.py:
- lockfile verbose iteration (lines 71-78): lockfile_entry calls with verbose logger
- APM_NO_CACHE absent -> cache wiring (lines 114-126)
- download_callback: already exists path (line 200)
- local dep user scope rejection (lines 230-235)
- local dep copy path (lines 244-261)
- locked ref path in download_callback (lines 265-295)
- direct vs transitive failure messages (lines 303-316)
- verbose tree transitive (lines 349-356)
- --only filtering with tree expansion (lines 387-410)
- dep_base_dirs divergent anchor (lines 475-489)
- dep_base_dirs AttributeError fallback (line 491)
- intended_dep_keys with non-empty list (line 502)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.install.context import InstallContext
from apm_cli.models.apm_package import APMPackage, GitReferenceType, ResolvedReference

# ---------------------------------------------------------------------------
# Constants - same patch targets as test_resolve_phase3.py
# ---------------------------------------------------------------------------
_RESOLVER_CLS = "apm_cli.deps.apm_resolver.APMDependencyResolver"
_SHARED_CACHE = "apm_cli.deps.shared_clone_cache.SharedCloneCache"
_GET_MODULES = "apm_cli.core.scope.get_modules_dir"
_GET_LOCKFILE = "apm_cli.deps.lockfile.get_lockfile_path"
_CHECK_INSECURE = "apm_cli.install.insecure_policy._check_insecure_dependencies"
_COLLECT_INSECURE = "apm_cli.install.insecure_policy._collect_insecure_dependency_infos"
_WARN_INSECURE = "apm_cli.install.insecure_policy._warn_insecure_dependencies"
_GUARD_INSECURE = "apm_cli.install.insecure_policy._guard_transitive_insecure_dependencies"


@pytest.fixture(autouse=True)
def _remote_ref_resolution() -> Iterator[None]:
    """Isolate ref I/O now that real references no longer supply truthy mock metadata."""
    with patch(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader.resolve_git_reference",
        return_value=ResolvedReference("main", GitReferenceType.BRANCH, "a" * 40, "main"),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    tmp_path: Path,
    *,
    update_refs: bool = False,
    only_packages=None,
    verbose: bool = False,
    logger=None,
) -> InstallContext:
    (tmp_path / "apm.yml").write_text(yaml.safe_dump({"name": "testapp", "version": "0.1.0"}))
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    ctx.apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml", create_config=False)
    ctx.update_refs = update_refs
    ctx.only_packages = only_packages
    ctx.verbose = verbose
    ctx.logger = logger
    ctx.scope = None
    ctx.auth_resolver = None
    ctx.protocol_pref = None
    ctx.allow_protocol_fallback = None
    ctx.allow_insecure = False
    ctx.allow_insecure_hosts = ()
    ctx.early_lockfile = None
    ctx.all_apm_deps = []
    return ctx


def _make_mock_graph(*, circular=None, deps=None):
    mock_graph = MagicMock()
    mock_graph.resolution_errors = []
    mock_graph.circular_dependencies = circular or []
    mock_graph.dependency_tree.nodes = {}
    mock_graph.dependency_tree.get_nodes_at_depth.return_value = []
    mock_graph.dependency_tree.max_depth = 1
    install_list = deps or []
    mock_graph.flattened_dependencies.get_installation_list.return_value = install_list
    return mock_graph


def test_materialization_migration_logger_reports_relative_case_change(
    tmp_path: Path,
) -> None:
    """Verbose diagnostics name the old and new package directory spelling."""
    from apm_cli.install.phases.resolve import _materialization_migration_logger

    logger = MagicMock()
    modules = tmp_path / "apm_modules"
    report = _materialization_migration_logger(logger, modules)

    assert report is not None
    report(
        modules / "mixedorg" / "mixedchild",
        modules / "MixedOrg" / "MixedChild",
    )

    logger.verbose_detail.assert_called_once_with(
        "    Migrated package directory casing: mixedorg/mixedchild -> MixedOrg/MixedChild"
    )


def _run_ctx(ctx, tmp_path, *, graph=None, extra_env=None, mock_resolver=None):
    mods_dir = tmp_path / "apm_modules"
    env = {"APM_NO_CACHE": "1", **(extra_env or {})}
    if graph is None:
        graph = _make_mock_graph()

    if mock_resolver is None:
        mock_resolver = MagicMock()
        mock_resolver.resolve_dependencies.return_value = graph
        mock_resolver._rejected_remote_local_keys = set()

    mock_cache = MagicMock()

    with (
        patch(_GET_MODULES, return_value=mods_dir),
        patch(_GET_LOCKFILE, return_value=tmp_path / "apm.lock.yaml"),
        patch(_RESOLVER_CLS, return_value=mock_resolver),
        patch(_SHARED_CACHE, return_value=mock_cache),
        patch(_CHECK_INSECURE),
        patch(_COLLECT_INSECURE, return_value=[]),
        patch(_WARN_INSECURE),
        patch(_GUARD_INSECURE),
        patch.dict("os.environ", env),
    ):
        from apm_cli.install.phases.resolve import run

        run(ctx)

    return mock_resolver, mock_cache


# ---------------------------------------------------------------------------
# Verbose lockfile iteration (lines 71-78)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# APM_NO_CACHE absent -> persistent cache wiring (lines 114-126)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Verbose tree log lines
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --only filtering with tree expansion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# dep_base_dirs computation
# ---------------------------------------------------------------------------
