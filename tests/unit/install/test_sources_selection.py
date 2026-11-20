"""Dependency source selection, cached-commit resolution and Materialization."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-locked-copy-validity")]

# ---------------------------------------------------------------------------
# Helpers: lightweight stubs
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    scope: Any = None,
    project_root: Path | None = None,
    targets: list[Any] | None = None,
    existing_lockfile: Any = None,
    callback_downloaded: dict[str, str] | None = None,
    logger: Any = None,
    tui: Any = None,
    update_refs: bool = False,
    registry_config: Any = None,
    auth_resolver: Any = None,
    expected_hash_change_deps: set[str] | None = None,
    content_hash_verified_deps: set[str] | None = None,
) -> MagicMock:
    """Build a minimal InstallContext-shaped MagicMock."""
    ctx = MagicMock()
    ctx.scope = scope
    ctx.project_root = project_root or Path("/fake/project")
    ctx.targets = targets if targets is not None else ["copilot"]
    ctx.existing_lockfile = existing_lockfile
    ctx.callback_downloaded = callback_downloaded or {}
    ctx.logger = logger
    ctx.tui = tui
    ctx.update_refs = update_refs
    ctx.registry_config = registry_config
    ctx.auth_resolver = auth_resolver
    ctx.expected_hash_change_deps = expected_hash_change_deps or set()
    ctx.content_hash_verified_deps = content_hash_verified_deps or set()
    ctx.installed_packages = []
    ctx.package_hashes = {}
    ctx.package_types = {}
    ctx.diagnostics = MagicMock()
    ctx.apm_modules_dir = Path("/fake/apm_modules")
    ctx.pre_download_results = {}

    # dependency_graph stub
    node_stub = MagicMock()
    node_stub.depth = 1
    node_stub.parent = None
    node_stub.is_dev = False
    ctx.dependency_graph.dependency_tree.get_node.return_value = node_stub

    return ctx


def _make_dep_ref(
    *,
    is_local: bool = False,
    local_path: str = "",
    is_virtual: bool = False,
    repo_url: str = "owner/repo",
    reference: str = "main",
    host: str = "github.com",
    port: Any = None,
) -> MagicMock:
    dep_ref = MagicMock()
    dep_ref.is_local = is_local
    dep_ref.local_path = local_path
    dep_ref.is_virtual = is_virtual
    dep_ref.repo_url = repo_url
    dep_ref.reference = reference
    dep_ref.host = host
    dep_ref.port = port
    return dep_ref


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


class TestMaterialization:
    """Tests for the Materialization dataclass."""

    def test_default_deltas(self) -> None:
        from apm_cli.install.sources import Materialization

        m = Materialization(
            package_info=None,
            install_path=Path("/some/path"),
            dep_key="owner/repo",
        )
        # A local copy is not a fetch; integration decides installed (#4184).
        assert m.deltas == {"installed": 0}

    def test_custom_deltas(self) -> None:
        from apm_cli.install.sources import Materialization

        m = Materialization(
            package_info=None,
            install_path=Path("/some/path"),
            dep_key="owner/repo",
            deltas={"installed": 1, "unpinned": 1},
        )
        assert m.deltas["unpinned"] == 1

    def test_install_path_stored(self) -> None:
        from apm_cli.install.sources import Materialization

        p = Path("/workspace/pkg")
        m = Materialization(package_info=MagicMock(), install_path=p, dep_key="a/b")
        assert m.install_path == p

    def test_dep_key_stored(self) -> None:
        from apm_cli.install.sources import Materialization

        m = Materialization(package_info=None, install_path=Path("/x"), dep_key="ns/name@1.0")
        assert m.dep_key == "ns/name@1.0"


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire
# ---------------------------------------------------------------------------


class TestLocalDependencySourceAcquire:
    """Tests for LocalDependencySource.acquire()."""

    def _make_source(
        self, ctx: MagicMock, dep_ref: MagicMock, install_path: Path, dep_key: str
    ) -> Any:
        from apm_cli.install.sources import LocalDependencySource

        return LocalDependencySource(ctx, dep_ref, install_path, dep_key)

    def test_user_scope_relative_path_returns_none(self) -> None:
        """USER scope + relative local_path → warn + return None."""
        from apm_cli.core.scope import InstallScope

        ctx = _make_ctx(scope=InstallScope.USER)
        dep_ref = _make_dep_ref(is_local=True, local_path="relative/pkg")
        source = self._make_source(ctx, dep_ref, Path("/fake/install"), "ns/pkg")

        result = source.acquire()

        assert result is None
        ctx.diagnostics.warn.assert_called_once()
        warn_msg = ctx.diagnostics.warn.call_args[0][0]
        assert "relative local paths" in warn_msg

    def test_user_scope_relative_path_with_logger_verbose(self) -> None:
        """USER scope + relative path + logger → verbose_detail called."""
        from apm_cli.core.scope import InstallScope

        mock_logger = MagicMock()
        ctx = _make_ctx(scope=InstallScope.USER, logger=mock_logger)
        dep_ref = _make_dep_ref(is_local=True, local_path="rel/path")
        source = self._make_source(ctx, dep_ref, Path("/fake/install"), "ns/rel")

        result = source.acquire()

        assert result is None
        mock_logger.verbose_detail.assert_called_once()


# ---------------------------------------------------------------------------
# CachedDependencySource._resolve_cached_commit
# ---------------------------------------------------------------------------


class TestResolveCachedCommit:
    """Tests for CachedDependencySource._resolve_cached_commit()."""

    def _make_source(
        self,
        ctx: MagicMock,
        dep_ref: MagicMock,
        *,
        resolved_ref: Any = None,
        dep_locked_chk: Any = None,
        fetched_this_run: bool = False,
    ) -> Any:
        from apm_cli.install.sources import CachedDependencySource

        return CachedDependencySource(
            ctx,
            dep_ref,
            Path("/fake/install"),
            "owner/repo",
            resolved_ref,
            dep_locked_chk,
            fetched_this_run=fetched_this_run,
        )

    def test_fetched_this_run_uses_callback_downloaded(self) -> None:
        """fetched_this_run=True → use callback_downloaded[dep_key]."""
        ctx = _make_ctx(callback_downloaded={"owner/repo": "abcdef1234567890"})
        dep_ref = _make_dep_ref()
        source = self._make_source(ctx, dep_ref, fetched_this_run=True)

        result = source._resolve_cached_commit()
        assert result == "abcdef1234567890"

    def test_fetched_this_run_fallback_to_resolved_ref(self) -> None:
        """fetched_this_run=True, no callback entry → resolved_ref.resolved_commit."""
        ctx = _make_ctx(callback_downloaded={})
        dep_ref = _make_dep_ref(reference="sha-from-ref")
        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "resolved_sha_abc"
        source = self._make_source(ctx, dep_ref, resolved_ref=resolved_ref, fetched_this_run=True)

        result = source._resolve_cached_commit()
        assert result == "resolved_sha_abc"

    def test_fetched_this_run_resolved_commit_is_cached_sentinel(self) -> None:
        """fetched_this_run=True, resolved_commit == 'cached' → falls back to dep_ref.reference."""
        ctx = _make_ctx(callback_downloaded={})
        dep_ref = _make_dep_ref(reference="main")
        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "cached"
        source = self._make_source(ctx, dep_ref, resolved_ref=resolved_ref, fetched_this_run=True)

        result = source._resolve_cached_commit()
        # "cached" sentinel is skipped → falls to dep_ref.reference
        assert result == "main"

    def test_cached_path_uses_existing_lockfile(self) -> None:
        """True cached path → trust the lockfile SHA."""
        locked_dep = MagicMock()
        locked_dep.resolved_commit = "lockfile_sha123"
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="fallback")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        assert result == "lockfile_sha123"

    def test_cached_path_lockfile_cached_sentinel_falls_back(self) -> None:
        """Lockfile SHA == 'cached' → falls back to dep_ref.reference."""
        locked_dep = MagicMock()
        locked_dep.resolved_commit = "cached"
        locked_dep.registry_prefix = None
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="main-branch")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        # "cached" is rejected → falls back
        assert result == "main-branch"

    def test_cached_proxy_row_has_no_commit_identity(self) -> None:
        """A registry-proxy row locks its ref name only, never a commit (#3657)."""
        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.registry_prefix = "artifactory/github"
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="1.0.0")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        assert source._resolve_cached_commit() is None

    def test_no_lockfile_falls_back_to_dep_ref_reference(self) -> None:
        """No existing_lockfile → dep_ref.reference used."""
        ctx = _make_ctx(existing_lockfile=None)
        dep_ref = _make_dep_ref(reference="v1.2.3")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        assert result == "v1.2.3"

    def test_locked_dep_is_none_falls_back(self) -> None:
        """Lockfile exists but get_dependency returns None → dep_ref.reference."""
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = None
        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="sha-xyz")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        assert result == "sha-xyz"

    def test_pre_downloaded_package_uses_materialized_commit(self) -> None:
        """Parallel pre-download identity wins over a mutable manifest ref."""
        ctx = _make_ctx(callback_downloaded={})
        package_info = MagicMock()
        package_info.resolved_reference.resolved_commit = "pre_downloaded_sha"
        ctx.pre_download_results = {"owner/repo": package_info}
        dep_ref = _make_dep_ref(reference="main")
        stale_resolved_ref = MagicMock(resolved_commit="stale_resolved_sha")
        source = self._make_source(
            ctx,
            dep_ref,
            resolved_ref=stale_resolved_ref,
            fetched_this_run=True,
        )

        result = source._resolve_cached_commit()

        assert result == "pre_downloaded_sha"

    def test_registry_dep_skips_reference_fallback(self) -> None:
        """Registry deps must not store dep_ref.reference as resolved_commit.

        dep_ref.reference is a semver range (e.g. '^1.0.0') for registry deps.
        Storing it as resolved_commit corrupts the lockfile and causes the
        update plan to show a spurious '^1.0.0 -> -' diff.
        """
        ctx = _make_ctx(existing_lockfile=None)
        dep_ref = _make_dep_ref(reference="^1.0.0")
        dep_ref.source = "registry"
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        assert result is None

    def test_registry_dep_with_lockfile_sha_still_uses_sha(self) -> None:
        """Registry dep: when lockfile has a real SHA, carry it forward."""
        locked_dep = MagicMock()
        locked_dep.resolved_commit = "abc1234def5678"
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="^1.0.0")
        dep_ref.source = "registry"
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        assert result == "abc1234def5678"


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FreshDependencySource.acquire
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# make_dependency_source factory
# ---------------------------------------------------------------------------


class TestMakeDependencySourceFactory:
    """Tests for make_dependency_source()."""

    def test_local_dep_returns_local_source(self) -> None:
        """is_local=True → LocalDependencySource."""
        from apm_cli.install.sources import LocalDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=True, local_path="/abs/path")
        source = make_dependency_source(ctx, dep_ref, Path("/install"), "k")
        assert isinstance(source, LocalDependencySource)

    def test_skip_download_returns_cached_source(self) -> None:
        """skip_download=True, not local → CachedDependencySource."""
        from apm_cli.install.sources import CachedDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        source = make_dependency_source(ctx, dep_ref, Path("/install"), "k", skip_download=True)
        assert isinstance(source, CachedDependencySource)

    def test_fresh_download_returns_fresh_source(self) -> None:
        """skip_download=False, not local → FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        source = make_dependency_source(ctx, dep_ref, Path("/install"), "k", skip_download=False)
        assert isinstance(source, FreshDependencySource)

    def test_fetched_this_run_forwarded_to_cached(self) -> None:
        """fetched_this_run=True forwarded to CachedDependencySource."""
        from apm_cli.install.sources import CachedDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        source = make_dependency_source(
            ctx, dep_ref, Path("/install"), "k", skip_download=True, fetched_this_run=True
        )
        assert isinstance(source, CachedDependencySource)
        assert source.fetched_this_run is True

    def test_local_dep_with_no_local_path_falls_through_to_fresh(self) -> None:
        """is_local=True but local_path='' (falsy) → FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=True, local_path="")
        source = make_dependency_source(ctx, dep_ref, Path("/install"), "k", skip_download=False)
        assert isinstance(source, FreshDependencySource)

    def test_progress_forwarded_to_fresh_source(self) -> None:
        """progress parameter is forwarded to FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        mock_progress = MagicMock()
        source = make_dependency_source(
            ctx, dep_ref, Path("/install"), "k", skip_download=False, progress=mock_progress
        )
        assert isinstance(source, FreshDependencySource)
        assert source.progress is mock_progress

    def test_resolved_ref_forwarded_to_cached(self) -> None:
        """resolved_ref passed to CachedDependencySource."""
        from apm_cli.install.sources import CachedDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        mock_ref = MagicMock()
        source = make_dependency_source(
            ctx, dep_ref, Path("/install"), "k", skip_download=True, resolved_ref=mock_ref
        )
        assert isinstance(source, CachedDependencySource)
        assert source.resolved_ref is mock_ref

    def test_dep_locked_chk_forwarded_to_fresh(self) -> None:
        """dep_locked_chk passed to FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        mock_chk = MagicMock()
        source = make_dependency_source(
            ctx, dep_ref, Path("/install"), "k", skip_download=False, dep_locked_chk=mock_chk
        )
        assert isinstance(source, FreshDependencySource)
        assert source.dep_locked_chk is mock_chk

    def test_ref_changed_forwarded_to_fresh(self) -> None:
        """ref_changed=True passed to FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=False)
        source = make_dependency_source(
            ctx, dep_ref, Path("/install"), "k", skip_download=False, ref_changed=True
        )
        assert isinstance(source, FreshDependencySource)
        assert source.ref_changed is True
