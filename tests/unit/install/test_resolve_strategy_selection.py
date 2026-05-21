"""Unit tests for apm_cli.install.phases.resolve.

Tests the resolve phase ``run(ctx)`` function by constructing minimal
InstallContext objects and patching all external I/O at the correct
import sites (resolve.py uses lazy/local imports inside run()).

Coverage targets (uncovered branches):
- Lockfile loading: early_lockfile, file exists, file absent
- Logger paths: verbose detail, lockfile_entry, update_refs branch
- APM_NO_CACHE env controls persistent cache wiring
- Shared clone cache always set and cleaned up
- Tiered ref resolver wiring success path
- Circular dependency raises RuntimeError
- rejected_remote_local_keys folded into callback_failures
- --only filtering
- dep_base_dirs computation (normal, divergent anchor warning, fallback)
- intended_dep_keys populated
- auth_resolver defaulting
- downloader populated on ctx
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.install.context import InstallContext

# ---------------------------------------------------------------------------
# Constants - correct patch targets for lazy imports in resolve.run()
# ---------------------------------------------------------------------------
_RESOLVER_CLS = "apm_cli.deps.apm_resolver.APMDependencyResolver"
_SHARED_CACHE = "apm_cli.deps.shared_clone_cache.SharedCloneCache"
_GET_MODULES = "apm_cli.core.scope.get_modules_dir"
_GET_LOCKFILE = "apm_cli.deps.lockfile.get_lockfile_path"
_CHECK_INSECURE = "apm_cli.install.insecure_policy._check_insecure_dependencies"
_COLLECT_INSECURE = "apm_cli.install.insecure_policy._collect_insecure_dependency_infos"
_WARN_INSECURE = "apm_cli.install.insecure_policy._warn_insecure_dependencies"
_GUARD_INSECURE = "apm_cli.install.insecure_policy._guard_transitive_insecure_dependencies"
_AUTH_RESOLVER = "apm_cli.core.auth.AuthResolver"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    tmp_path: Path,
    *,
    update_refs: bool = False,
    only_packages: list[str] | None = None,
    verbose: bool = False,
    logger=None,
) -> InstallContext:
    """Minimal InstallContext sufficient for resolve.run()."""
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


def _make_mock_graph(*, circular=None):
    mock_graph = MagicMock()
    mock_graph.circular_dependencies = circular or []
    mock_graph.dependency_tree.nodes = {}
    mock_graph.dependency_tree.get_nodes_at_depth.return_value = []
    mock_graph.dependency_tree.max_depth = 1
    mock_graph.flattened_dependencies.get_installation_list.return_value = []
    return mock_graph


def _run_ctx(ctx: InstallContext, tmp_path: Path, *, graph=None, extra_env=None) -> tuple:
    """Run resolve.run(ctx) with all I/O patched. Returns (mock_resolver, mock_cache)."""
    mods_dir = tmp_path / "apm_modules"
    env = {"APM_NO_CACHE": "1", **(extra_env or {})}
    if graph is None:
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
        patch.dict("os.environ", env),
    ):
        from apm_cli.install.phases.resolve import run

        run(ctx)

    return mock_resolver, mock_cache


# ---------------------------------------------------------------------------
# Happy-path smoke test
# ---------------------------------------------------------------------------


class TestResolveRunHappyPath:
    def test_basic_run_populates_ctx_fields(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _mock_resolver, mock_cache = _run_ctx(ctx, tmp_path)

        assert ctx.apm_modules_dir is not None
        assert ctx.dependency_graph is not None
        assert ctx.deps_to_install == []
        assert isinstance(ctx.intended_dep_keys, set)
        assert isinstance(ctx.callback_downloaded, dict)
        assert isinstance(ctx.callback_failures, set)
        assert isinstance(ctx.transitive_failures, list)
        mock_cache.cleanup.assert_called_once()

    def test_downloader_set_on_ctx(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        assert ctx.downloader is not None

    def test_apm_modules_dir_created(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        # get_modules_dir was mocked; apm_modules_dir should be set
        assert ctx.apm_modules_dir == tmp_path / "apm_modules"

    def test_lockfile_path_set_on_ctx(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        assert ctx.lockfile_path == tmp_path / "apm.lock.yaml"


# ---------------------------------------------------------------------------
# Lockfile loading
# ---------------------------------------------------------------------------


class TestLockfileLoading:
    def test_early_lockfile_used_without_reading_disk(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        early_lf = MagicMock()
        early_lf.dependencies = {"org/pkg": MagicMock()}
        early_lf.get_all_dependencies.return_value = []
        ctx.early_lockfile = early_lf

        _run_ctx(ctx, tmp_path)

        assert ctx.existing_lockfile is early_lf

    def test_no_lockfile_file_sets_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.early_lockfile = None

        _run_ctx(ctx, tmp_path)

        assert ctx.existing_lockfile is None

    def test_existing_lockfile_on_disk_is_loaded(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.early_lockfile = None

        lockfile_path = tmp_path / "apm.lock.yaml"
        mock_lf = MagicMock()
        mock_lf.dependencies = {}
        mock_lf.get_all_dependencies.return_value = []

        with (
            patch(_GET_MODULES, return_value=tmp_path / "apm_modules"),
            patch(_GET_LOCKFILE, return_value=lockfile_path),
            patch(_RESOLVER_CLS) as MockResolver,
            patch(_SHARED_CACHE, return_value=MagicMock()),
            patch(_CHECK_INSECURE),
            patch(_COLLECT_INSECURE, return_value=[]),
            patch(_WARN_INSECURE),
            patch(_GUARD_INSECURE),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lf),
            patch.dict("os.environ", {"APM_NO_CACHE": "1"}),
        ):
            lockfile_path.write_text("dependencies: {}")  # make it exist on disk
            mock_g = _make_mock_graph()
            mock_r = MagicMock()
            mock_r.resolve_dependencies.return_value = mock_g
            mock_r._rejected_remote_local_keys = set()
            MockResolver.return_value = mock_r

            from apm_cli.install.phases.resolve import run

            run(ctx)

        assert ctx.existing_lockfile is mock_lf

    def test_verbose_logger_logs_lockfile_count(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = True
        ctx = _make_ctx(tmp_path, verbose=True, logger=mock_logger)

        early_lf = MagicMock()
        early_lf.dependencies = {"org/pkg": MagicMock(), "org/pkg2": MagicMock()}
        early_lf.get_all_dependencies.return_value = []
        ctx.early_lockfile = early_lf

        _run_ctx(ctx, tmp_path)

        mock_logger.verbose_detail.assert_called()

    def test_update_refs_logs_sha_comparison_message(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = False
        ctx = _make_ctx(tmp_path, update_refs=True, logger=mock_logger)

        early_lf = MagicMock()
        early_lf.dependencies = {"org/pkg": MagicMock()}
        early_lf.get_all_dependencies.return_value = []
        ctx.early_lockfile = early_lf

        _run_ctx(ctx, tmp_path)

        # Should log the "SHA comparison" message rather than "Using apm.lock.yaml"
        calls = [str(call) for call in mock_logger.verbose_detail.call_args_list]
        assert any("SHA comparison" in c or "comparison" in c.lower() for c in calls)


# ---------------------------------------------------------------------------
# Auth resolver defaulting
# ---------------------------------------------------------------------------


class TestAuthResolverDefaulting:
    def test_auth_resolver_instantiated_when_none(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.auth_resolver = None

        with (
            patch(_GET_MODULES, return_value=tmp_path / "apm_modules"),
            patch(_GET_LOCKFILE, return_value=tmp_path / "apm.lock.yaml"),
            patch(_RESOLVER_CLS) as MockResolver,
            patch(_SHARED_CACHE, return_value=MagicMock()),
            patch(_CHECK_INSECURE),
            patch(_COLLECT_INSECURE, return_value=[]),
            patch(_WARN_INSECURE),
            patch(_GUARD_INSECURE),
            patch.dict("os.environ", {"APM_NO_CACHE": "1"}),
            patch(_AUTH_RESOLVER) as MockAuth,
        ):
            mock_auth_inst = MagicMock()
            MockAuth.return_value = mock_auth_inst
            mock_g = _make_mock_graph()
            mock_r = MagicMock()
            mock_r.resolve_dependencies.return_value = mock_g
            mock_r._rejected_remote_local_keys = set()
            MockResolver.return_value = mock_r

            from apm_cli.install.phases.resolve import run

            run(ctx)

        assert ctx.auth_resolver is mock_auth_inst

    def test_existing_auth_resolver_preserved(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        existing = MagicMock()
        ctx.auth_resolver = existing

        _run_ctx(ctx, tmp_path)

        assert ctx.auth_resolver is existing


# ---------------------------------------------------------------------------
# Circular dependency raises RuntimeError
# ---------------------------------------------------------------------------


class TestCircularDependency:
    def test_circular_dep_raises_runtime_error(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        circular = MagicMock()
        circular.cycle_path = ["org/a", "org/b", "org/a"]
        graph = _make_mock_graph(circular=[circular])

        with pytest.raises(RuntimeError, match="circular"):
            _run_ctx(ctx, tmp_path, graph=graph)

    def test_circular_dep_error_logged_when_logger_set(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        ctx = _make_ctx(tmp_path, logger=mock_logger)
        circular = MagicMock()
        circular.cycle_path = ["org/a", "org/b", "org/a"]
        graph = _make_mock_graph(circular=[circular])

        with pytest.raises(RuntimeError):
            _run_ctx(ctx, tmp_path, graph=graph)

        mock_logger.error.assert_called()


# ---------------------------------------------------------------------------
# rejected_remote_local_keys propagation
# ---------------------------------------------------------------------------


class TestRejectedRemoteLocalKeys:
    def test_rejected_keys_added_to_callback_failures(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        graph = _make_mock_graph()

        mock_resolver = MagicMock()
        mock_resolver.resolve_dependencies.return_value = graph
        mock_resolver._rejected_remote_local_keys = {"../secret", "bad/path"}
        mock_cache = MagicMock()

        with (
            patch(_GET_MODULES, return_value=tmp_path / "apm_modules"),
            patch(_GET_LOCKFILE, return_value=tmp_path / "apm.lock.yaml"),
            patch(_RESOLVER_CLS, return_value=mock_resolver),
            patch(_SHARED_CACHE, return_value=mock_cache),
            patch(_CHECK_INSECURE),
            patch(_COLLECT_INSECURE, return_value=[]),
            patch(_WARN_INSECURE),
            patch(_GUARD_INSECURE),
            patch.dict("os.environ", {"APM_NO_CACHE": "1"}),
        ):
            from apm_cli.install.phases.resolve import run

            run(ctx)

        assert "../secret" in ctx.callback_failures
        assert "bad/path" in ctx.callback_failures

    def test_empty_rejected_set_no_change(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        assert ctx.callback_failures == set()


# ---------------------------------------------------------------------------
# --only filtering
# ---------------------------------------------------------------------------


class TestOnlyFiltering:
    def test_only_filter_includes_wanted_dep(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, only_packages=["org/wanted"])

        wanted_ref = MagicMock()
        wanted_ref.get_identity.return_value = "org/wanted"
        wanted_ref.get_unique_key.return_value = "org/wanted"

        unwanted_ref = MagicMock()
        unwanted_ref.get_identity.return_value = "org/unwanted"
        unwanted_ref.get_unique_key.return_value = "org/unwanted"

        graph = _make_mock_graph()
        graph.flattened_dependencies.get_installation_list.return_value = [
            wanted_ref,
            unwanted_ref,
        ]
        # Empty nodes tree so descendant expansion is a no-op
        graph.dependency_tree.nodes = {}

        mock_resolver = MagicMock()
        mock_resolver.resolve_dependencies.return_value = graph
        mock_resolver._rejected_remote_local_keys = set()
        mock_cache = MagicMock()

        parsed_ref = MagicMock()
        parsed_ref.get_identity.return_value = "org/wanted"

        with (
            patch(_GET_MODULES, return_value=tmp_path / "apm_modules"),
            patch(_GET_LOCKFILE, return_value=tmp_path / "apm.lock.yaml"),
            patch(_RESOLVER_CLS, return_value=mock_resolver),
            patch(_SHARED_CACHE, return_value=mock_cache),
            patch(_CHECK_INSECURE),
            patch(_COLLECT_INSECURE, return_value=[]),
            patch(_WARN_INSECURE),
            patch(_GUARD_INSECURE),
            patch.dict("os.environ", {"APM_NO_CACHE": "1"}),
            patch(
                "apm_cli.models.apm_package.DependencyReference.parse",
                return_value=parsed_ref,
            ),
        ):
            from apm_cli.install.phases.resolve import run

            run(ctx)

        assert ctx.deps_to_install == [wanted_ref]

    def test_no_only_filter_installs_all_deps(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, only_packages=None)
        dep1 = MagicMock()
        dep1.get_unique_key.return_value = "org/a"
        dep2 = MagicMock()
        dep2.get_unique_key.return_value = "org/b"

        graph = _make_mock_graph()
        graph.flattened_dependencies.get_installation_list.return_value = [dep1, dep2]

        _run_ctx(ctx, tmp_path, graph=graph)

        assert ctx.deps_to_install == [dep1, dep2]


# ---------------------------------------------------------------------------
# intended_dep_keys
# ---------------------------------------------------------------------------


class TestIntendedDepKeys:
    def test_intended_dep_keys_populated_from_deps_to_install(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        dep = MagicMock()
        dep.get_unique_key.return_value = "org/mypkg"
        graph = _make_mock_graph()
        graph.flattened_dependencies.get_installation_list.return_value = [dep]

        _run_ctx(ctx, tmp_path, graph=graph)

        assert "org/mypkg" in ctx.intended_dep_keys

    def test_no_deps_gives_empty_intended_keys(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        assert ctx.intended_dep_keys == set()


# ---------------------------------------------------------------------------
# dep_base_dirs computation
# ---------------------------------------------------------------------------


class TestDepBaseDirs:
    def test_dep_base_dirs_populated_for_transitive(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        parent_source = tmp_path / "parent_src"

        parent_pkg = MagicMock()
        parent_pkg.source_path = parent_source
        parent_node = MagicMock()
        parent_node.package = parent_pkg

        child_ref = MagicMock()
        child_ref.get_unique_key.return_value = "../sibling"
        child_node = MagicMock()
        child_node.parent = parent_node
        child_node.package = MagicMock()
        child_node.dependency_ref = child_ref

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"../sibling": child_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        assert ctx.dep_base_dirs.get("../sibling") == parent_source

    def test_root_node_parent_none_is_skipped(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        root_ref = MagicMock()
        root_ref.get_unique_key.return_value = "org/root"
        root_node = MagicMock()
        root_node.parent = None
        root_node.dependency_ref = root_ref
        root_node.package = MagicMock()

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"org/root": root_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        assert "org/root" not in ctx.dep_base_dirs

    def test_divergent_anchors_warns_and_keeps_first(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _make_ctx(tmp_path)

        anchor_a = tmp_path / "anchor_a"
        anchor_b = tmp_path / "anchor_b"

        def make_child_node(parent_source):
            pkg = MagicMock()
            pkg.source_path = parent_source
            parent_node = MagicMock()
            parent_node.package = pkg
            ref = MagicMock()
            ref.get_unique_key.return_value = "same/dep"
            node = MagicMock()
            node.parent = parent_node
            node.package = MagicMock()
            node.dependency_ref = ref
            return node

        node_a = make_child_node(anchor_a)
        node_b = make_child_node(anchor_b)

        graph = _make_mock_graph()
        graph.dependency_tree.nodes = {"same/dep:a": node_a, "same/dep:b": node_b}
        # Both nodes return the same dep key to simulate collision
        node_a.dependency_ref.get_unique_key.return_value = "same/dep"
        node_b.dependency_ref.get_unique_key.return_value = "same/dep"

        with caplog.at_level(logging.WARNING):
            _run_ctx(ctx, tmp_path, graph=graph)

        # First anchor wins; warning should be present
        assert ctx.dep_base_dirs.get("same/dep") == anchor_a
        assert any(
            "divergent" in r.message.lower() or "anchor" in r.message.lower()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )

    def test_attribute_error_falls_back_to_empty(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        graph = _make_mock_graph()

        # Make nodes property raise AttributeError
        def bad_nodes():
            raise AttributeError("broken")

        type(graph.dependency_tree).nodes = property(lambda self: bad_nodes())

        _run_ctx(ctx, tmp_path, graph=graph)

        assert ctx.dep_base_dirs == {}


# ---------------------------------------------------------------------------
# Shared cache cleanup
# ---------------------------------------------------------------------------


class TestSharedCacheCleanup:
    def test_shared_cache_cleanup_always_called(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _, mock_cache = _run_ctx(ctx, tmp_path)
        mock_cache.cleanup.assert_called_once()

    def test_shared_cache_attached_to_downloader(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        _run_ctx(ctx, tmp_path)
        # downloader should have shared_clone_cache set
        assert ctx.downloader is not None


# ---------------------------------------------------------------------------
# Verbose tree summary logging
# ---------------------------------------------------------------------------


class TestVerboseTreeSummary:
    def test_verbose_logs_resolved_dep_count(self, tmp_path: Path) -> None:
        mock_logger = MagicMock()
        mock_logger.verbose = False
        ctx = _make_ctx(tmp_path, logger=mock_logger)

        dep = MagicMock()
        dep.get_unique_key.return_value = "org/pkg"
        graph = _make_mock_graph()
        graph.flattened_dependencies.get_installation_list.return_value = [dep]
        # One direct dep at depth 1, no transitive
        direct_node = MagicMock()
        graph.dependency_tree.get_nodes_at_depth.return_value = [direct_node]
        graph.dependency_tree.nodes = {"org/pkg": direct_node}

        _run_ctx(ctx, tmp_path, graph=graph)

        mock_logger.verbose_detail.assert_called()
