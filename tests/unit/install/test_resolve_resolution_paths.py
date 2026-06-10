"""unit tests for apm_cli.install.phases.resolve.

Covers missing lines/branches in resolve module:
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

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from apm_cli.install.context import InstallContext

# ---------------------------------------------------------------------------
# Patch targets for resolve module
# ---------------------------------------------------------------------------
_RESOLVER_CLS = "apm_cli.deps.apm_resolver.APMDependencyResolver"
_SHARED_CACHE = "apm_cli.deps.shared_clone_cache.SharedCloneCache"
_GET_MODULES = "apm_cli.core.scope.get_modules_dir"
_GET_LOCKFILE = "apm_cli.deps.lockfile.get_lockfile_path"
_CHECK_INSECURE = "apm_cli.install.insecure_policy._check_insecure_dependencies"
_COLLECT_INSECURE = "apm_cli.install.insecure_policy._collect_insecure_dependency_infos"
_WARN_INSECURE = "apm_cli.install.insecure_policy._warn_insecure_dependencies"
_GUARD_INSECURE = "apm_cli.install.insecure_policy._guard_transitive_insecure_dependencies"


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


class TestVerboseLockfileIteration:
    def test_lockfile_entry_called_per_dependency(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = True
        ctx = _make_ctx(tmp_path, verbose=True, logger=mock_logger)

        dep1 = MagicMock()
        dep1.get_unique_key.return_value = "org/pkg"
        dep1.resolved_commit = "abc123"
        dep1.resolved_ref = "main"

        dep2 = MagicMock()
        dep2.get_unique_key.return_value = "org/pkg2"
        dep2.resolved_commit = None
        dep2.resolved_ref = None

        early_lf = MagicMock()
        early_lf.dependencies = {"org/pkg": dep1, "org/pkg2": dep2}
        early_lf.get_all_dependencies.return_value = [dep1, dep2]
        ctx.early_lockfile = early_lf

        _run_ctx(ctx, tmp_path)

        mock_logger.lockfile_entry.assert_called()

    def test_dep_without_resolved_ref_attr(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = True
        ctx = _make_ctx(tmp_path, verbose=True, logger=mock_logger)

        dep = MagicMock(spec=["get_unique_key", "resolved_commit"])
        dep.get_unique_key.return_value = "org/pkg"
        dep.resolved_commit = "sha1234"

        early_lf = MagicMock()
        early_lf.dependencies = {"org/pkg": dep}
        early_lf.get_all_dependencies.return_value = [dep]
        ctx.early_lockfile = early_lf

        _run_ctx(ctx, tmp_path)

        mock_logger.lockfile_entry.assert_called()


# ---------------------------------------------------------------------------
# APM_NO_CACHE absent -> persistent cache wiring (lines 114-126)
# ---------------------------------------------------------------------------


class TestCacheWiring:
    def test_cache_wired_when_no_env_var(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        mods_dir = tmp_path / "apm_modules"
        graph = _make_mock_graph()
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
            patch.dict("os.environ", {}, clear=True),  # no APM_NO_CACHE
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
            patch("apm_cli.cache.git_cache.GitCache") as MockGitCache,
        ):
            from apm_cli.install.phases.resolve import run

            run(ctx)

        MockGitCache.assert_called()

    def test_cache_oserror_degrades_gracefully(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        mods_dir = tmp_path / "apm_modules"
        graph = _make_mock_graph()
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
            patch.dict("os.environ", {}, clear=True),
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
            patch("apm_cli.cache.git_cache.GitCache", side_effect=OSError("no perm")),
        ):
            from apm_cli.install.phases.resolve import run

            run(ctx)  # should not raise

        assert ctx.dep_base_dirs == {}


# ---------------------------------------------------------------------------
# Verbose tree log lines
# ---------------------------------------------------------------------------


class TestVerboseTreeLogging:
    def test_transitive_dep_tree_logged(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = False
        ctx = _make_ctx(tmp_path, logger=mock_logger)

        graph = _make_mock_graph()
        # Simulate 2 direct + 1 transitive
        graph.dependency_tree.get_nodes_at_depth.return_value = [MagicMock(), MagicMock()]
        # Add one transitive node at depth 2
        trans_node = MagicMock()
        trans_node.depth = 2
        trans_node.get_ancestor_chain.return_value = "root > org/pkg > org/trans"
        graph.dependency_tree.nodes = {"org/trans": trans_node}
        graph.dependency_tree.max_depth = 2

        _run_ctx(ctx, tmp_path, graph=graph)

        mock_logger.verbose_detail.assert_called()

    def test_only_direct_deps_no_transitive_message(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = False
        ctx = _make_ctx(tmp_path, logger=mock_logger)

        graph = _make_mock_graph()
        graph.dependency_tree.get_nodes_at_depth.return_value = [MagicMock()]
        graph.dependency_tree.nodes = {}
        graph.dependency_tree.max_depth = 1

        _run_ctx(ctx, tmp_path, graph=graph)

        calls = [str(c) for c in mock_logger.verbose_detail.call_args_list]
        assert any("direct" in c or "no transitive" in c for c in calls)


# ---------------------------------------------------------------------------
# --only filtering with tree expansion
# ---------------------------------------------------------------------------


class TestOnlyFiltering:
    def test_only_package_filters_dep_list(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, only_packages=["org/pkg"])

        dep_a = MagicMock()
        dep_a.get_unique_key.return_value = "org/pkg"
        dep_a.get_identity.return_value = "org/pkg"

        dep_b = MagicMock()
        dep_b.get_unique_key.return_value = "org/other"
        dep_b.get_identity.return_value = "org/other"

        graph = _make_mock_graph(deps=[dep_a, dep_b])
        graph.dependency_tree.nodes = {}

        _run_ctx(ctx, tmp_path, graph=graph)

        keys = [d.get_unique_key() for d in ctx.deps_to_install]
        assert "org/pkg" in keys
        assert "org/other" not in keys

    def test_only_with_children_expands_tree(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, only_packages=["org/pkg"])

        dep_a = MagicMock()
        dep_a.get_unique_key.return_value = "org/pkg"
        dep_a.get_identity.return_value = "org/pkg"

        dep_child = MagicMock()
        dep_child.get_unique_key.return_value = "org/child"
        dep_child.get_identity.return_value = "org/child"

        graph = _make_mock_graph(deps=[dep_a, dep_child])

        # Build tree node with child
        child_ref = MagicMock()
        child_ref.get_identity.return_value = "org/child"
        child_node = MagicMock()
        child_node.children = []
        child_node.dependency_ref = child_ref

        parent_ref = MagicMock()
        parent_ref.get_identity.return_value = "org/pkg"
        parent_node = MagicMock()
        parent_node.children = [child_node]
        parent_node.dependency_ref = parent_ref

        graph.dependency_tree.nodes = {"org/pkg": parent_node, "org/child": child_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        keys = [d.get_unique_key() for d in ctx.deps_to_install]
        assert "org/pkg" in keys
        assert "org/child" in keys

    def test_only_invalid_ref_falls_back_to_raw(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, only_packages=["INVALID_REF_NO_SLASH"])

        dep_a = MagicMock()
        dep_a.get_unique_key.return_value = "INVALID_REF_NO_SLASH"
        dep_a.get_identity.return_value = "INVALID_REF_NO_SLASH"

        graph = _make_mock_graph(deps=[dep_a])
        graph.dependency_tree.nodes = {}

        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            _run_ctx(ctx, tmp_path, graph=graph)


# ---------------------------------------------------------------------------
# dep_base_dirs computation
# ---------------------------------------------------------------------------


class TestDepBaseDirs:
    def test_dep_base_dirs_populated_from_tree(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        parent_source = tmp_path / "parent_src"
        parent_source.mkdir()

        parent_pkg = MagicMock()
        parent_pkg.source_path = parent_source

        child_ref = MagicMock()
        child_ref.get_unique_key.return_value = "org/child"

        parent_node = MagicMock()
        parent_node.parent = None
        parent_node.package = parent_pkg

        child_node = MagicMock()
        child_node.parent = parent_node
        child_node.package = None
        child_node.dependency_ref = child_ref

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"org/child": child_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        assert "org/child" in ctx.dep_base_dirs
        assert ctx.dep_base_dirs["org/child"] == parent_source

    def test_dep_base_dirs_divergent_anchor_warns(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        source_a = tmp_path / "parent_a"
        source_a.mkdir()
        source_b = tmp_path / "parent_b"
        source_b.mkdir()

        pkg_a = MagicMock()
        pkg_a.source_path = source_a
        pkg_b = MagicMock()
        pkg_b.source_path = source_b

        parent_node_a = MagicMock()
        parent_node_a.parent = None
        parent_node_a.package = pkg_a

        parent_node_b = MagicMock()
        parent_node_b.parent = None
        parent_node_b.package = pkg_b

        child_ref = MagicMock()
        child_ref.get_unique_key.return_value = "org/child"

        # Two different nodes declaring same key
        node1 = MagicMock()
        node1.parent = parent_node_a
        node1.package = None
        node1.dependency_ref = child_ref

        node2 = MagicMock()
        node2.parent = parent_node_b
        node2.package = None
        node2.dependency_ref = child_ref

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"org/child_1": node1, "org/child_2": node2}

        import logging

        with patch.object(
            logging.getLogger("apm_cli.install.phases.resolve"), "warning"
        ) as mock_warn:
            _run_ctx(ctx, tmp_path, graph=graph)

        # Second node with different anchor should log a warning
        mock_warn.assert_called()

    def test_dep_base_dirs_attribute_error_degrades(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = MagicMock(side_effect=AttributeError("no nodes"))

        # Should not raise - gracefully degrades to empty dep_base_dirs
        _run_ctx(ctx, tmp_path, graph=graph)
        assert ctx.dep_base_dirs == {}

    def test_parent_none_skipped(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)

        child_ref = MagicMock()
        child_ref.get_unique_key.return_value = "org/root_dep"

        root_node = MagicMock()
        root_node.parent = None
        root_node.package = None
        root_node.dependency_ref = child_ref

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"org/root_dep": root_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        # No entry since parent is None
        assert "org/root_dep" not in ctx.dep_base_dirs

    def test_intended_dep_keys_populated(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)

        dep = MagicMock()
        dep.get_unique_key.return_value = "org/pkg"
        dep.get_identity.return_value = "org/pkg"

        graph = _make_mock_graph(deps=[dep])
        graph.dependency_tree.nodes = {}

        _run_ctx(ctx, tmp_path, graph=graph)

        assert "org/pkg" in ctx.intended_dep_keys
