"""Unit tests for apm_cli.install.sources.

Covers the DependencySource strategy classes and their helpers that are not
yet reached by test_sources_classification.py:

* Materialization dataclass - field defaults and custom deltas
* LocalDependencySource.acquire
  - USER-scope + relative path → skip + return None
  - USER-scope + absolute path → allowed
  - _copy_local_package failure → return None
  - success path with apm.yml present
  - success path without apm.yml (bare APMPackage)
  - relative local_path resolution
  - package_type detection (MARKETPLACE_PLUGIN branch)
* CachedDependencySource._resolve_cached_commit
  - fetched_this_run=True with callback_downloaded entry
  - fetched_this_run=True with resolved_ref fallback
  - cached path from existing_lockfile
  - fallback to dep_ref.reference
* CachedDependencySource.acquire
  - no targets → package_info=None materialization
  - with targets + apm.yml present
  - with targets + no apm.yml (bare APMPackage)
  - resolved_ref=None branch (ResolvedReference created)
* FreshDependencySource.acquire
  - success path (no progress, no tui, no logger)
  - success path with logger
  - success path with progress object
  - unpinned dep (reference=None → deltas["unpinned"]=1)
  - hash mismatch → sys.exit(1)
  - exception in download → diagnostics.error + return None
  - no targets path
* make_dependency_source factory
  - local dep → LocalDependencySource
  - skip_download=True → CachedDependencySource
  - skip_download=False → FreshDependencySource
  - fetched_this_run forwarded to CachedDependencySource
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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
        assert m.deltas == {"installed": 1}

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

    def test_copy_failure_records_error_and_returns_none(self, tmp_path: Path) -> None:
        """_copy_local_package returning None → diagnostics.error + None."""
        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path))
        install_path = tmp_path / "install"
        install_path.mkdir()

        with patch("apm_cli.install.sources.LocalDependencySource.acquire", wraps=None) as _w:
            pass  # just to ensure the import works

        with (
            patch("apm_cli.install.phases.local_content._copy_local_package", return_value=None),
            patch("apm_cli.core.scope.InstallScope") as _is,
        ):
            source = self._make_source(ctx, dep_ref, install_path, "owner/pkg")
            result = source.acquire()

        assert result is None
        ctx.diagnostics.error.assert_called_once()

    def test_success_without_apm_yml(self, tmp_path: Path) -> None:
        """Success path: no apm.yml in install_path → bare APMPackage."""
        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.models.validation.detect_package_type",
                return_value=(None, None),
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc123",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            result = source.acquire()

        assert result is not None
        assert result.dep_key == "owner/pkg"
        assert result.install_path == install_path

    def test_success_with_apm_yml(self, tmp_path: Path) -> None:
        """Success path: apm.yml present → APMPackage.from_apm_yml called."""
        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(
            is_local=True, local_path=str(tmp_path / "src_pkg"), reference="local"
        )
        install_path = tmp_path / "install"
        install_path.mkdir()

        # Create a minimal apm.yml in the install path
        (install_path / "apm.yml").write_text("name: mypkg\nversion: 0.1.0\n")

        mock_pkg = MagicMock()
        mock_pkg.source = None

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                return_value=mock_pkg,
            ),
            patch(
                "apm_cli.models.validation.detect_package_type",
                return_value=(None, None),
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="deadbeef",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            result = source.acquire()

        assert result is not None

    def test_logger_download_complete_called_on_success(self, tmp_path: Path) -> None:
        """logger.download_complete should be called on the happy path."""
        mock_logger = MagicMock()
        ctx = _make_ctx(project_root=tmp_path, logger=mock_logger)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.models.validation.detect_package_type",
                return_value=(None, None),
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            source.acquire()

        mock_logger.download_complete.assert_called_once()

    def test_marketplace_plugin_normalise_called(self, tmp_path: Path) -> None:
        """MARKETPLACE_PLUGIN package type triggers normalize_plugin_directory."""
        from apm_cli.models.apm_package import PackageType

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        fake_plugin_json = install_path / "plugin.json"

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.models.validation.detect_package_type",
                return_value=(PackageType.MARKETPLACE_PLUGIN, fake_plugin_json),
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc",
            ),
            patch("apm_cli.deps.plugin_parser.normalize_plugin_directory") as mock_normalise,
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/plugin")
            result = source.acquire()

        mock_normalise.assert_called_once_with(install_path, fake_plugin_json)
        assert result is not None


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
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        dep_ref = _make_dep_ref(reference="main-branch")
        source = self._make_source(ctx, dep_ref, fetched_this_run=False)

        result = source._resolve_cached_commit()
        # "cached" is rejected → falls back
        assert result == "main-branch"

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


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire
# ---------------------------------------------------------------------------


class TestCachedDependencySourceAcquire:
    """Tests for CachedDependencySource.acquire()."""

    def _make_source(
        self,
        ctx: MagicMock,
        dep_ref: MagicMock,
        install_path: Path,
        dep_key: str = "owner/repo",
        *,
        resolved_ref: Any = None,
        dep_locked_chk: Any = None,
        fetched_this_run: bool = False,
    ) -> Any:
        from apm_cli.install.sources import CachedDependencySource

        return CachedDependencySource(
            ctx,
            dep_ref,
            install_path,
            dep_key,
            resolved_ref,
            dep_locked_chk,
            fetched_this_run=fetched_this_run,
        )

    def test_no_targets_returns_none_package_info(self, tmp_path: Path) -> None:
        """ctx.targets=[] → Materialization with package_info=None."""
        ctx = _make_ctx(targets=[])
        dep_ref = _make_dep_ref(is_virtual=False)
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc"

        with patch(
            "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
            return_value="abc",
        ):
            source = self._make_source(ctx, dep_ref, install_path, dep_locked_chk=locked_chk)
            result = source.acquire()

        assert result is not None
        assert result.package_info is None

    def test_with_targets_and_apm_yml(self, tmp_path: Path) -> None:
        """Happy path: apm.yml present → full Materialization returned."""
        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "apm.yml").write_text("name: mypkg\nversion: 0.1.0\n")

        mock_pkg = MagicMock()
        mock_pkg.source = "owner/repo"

        with (
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml", return_value=mock_pkg),
            patch("apm_cli.models.validation.detect_package_type", return_value=(None, None)),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="hash1"),
            patch(
                "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
                return_value="commit_sha",
            ),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.dep_key == "owner/repo"

    def test_with_targets_no_apm_yml(self, tmp_path: Path) -> None:
        """No apm.yml → bare APMPackage created."""
        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, repo_url="owner/barerepo", reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        # No apm.yml

        with (
            patch("apm_cli.models.validation.detect_package_type", return_value=(None, None)),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="hash2"),
            patch(
                "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
                return_value="commit",
            ),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is not None

    def test_resolved_ref_none_creates_fallback_resolved_reference(self, tmp_path: Path) -> None:
        """resolved_ref=None → a synthetic ResolvedReference is built."""
        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, reference="feature-branch")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "apm.yml").write_text("name: test\nversion: 0.0.1\n")

        mock_pkg = MagicMock()
        mock_pkg.source = "owner/repo"

        with (
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml", return_value=mock_pkg),
            patch("apm_cli.models.validation.detect_package_type", return_value=(None, None)),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch(
                "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
                return_value="c",
            ),
        ):
            source = self._make_source(ctx, dep_ref, install_path, resolved_ref=None)
            result = source.acquire()

        assert result is not None
        # resolved_ref was None; the code builds a fallback - we just confirm no crash

    def test_unpinned_dep_sets_delta(self, tmp_path: Path) -> None:
        """dep_ref.reference=None → deltas['unpinned']=1."""
        ctx = _make_ctx(targets=[])
        dep_ref = _make_dep_ref(is_virtual=False, reference=None)
        dep_ref.reference = None  # explicit None
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc"

        with patch(
            "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
            return_value="abc",
        ):
            source = self._make_source(ctx, dep_ref, install_path, dep_locked_chk=locked_chk)
            result = source.acquire()

        assert result is not None
        assert result.deltas.get("unpinned") == 1

    def test_logger_download_complete_called(self, tmp_path: Path) -> None:
        """logger.download_complete should be called with cached info."""
        mock_logger = MagicMock()
        ctx = _make_ctx(targets=[], logger=mock_logger)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "sha1"
        locked_chk.registry_prefix = None

        with patch(
            "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
            return_value="sha1",
        ):
            source = self._make_source(ctx, dep_ref, install_path, dep_locked_chk=locked_chk)
            source.acquire()

        mock_logger.download_complete.assert_called_once()


# ---------------------------------------------------------------------------
# FreshDependencySource.acquire
# ---------------------------------------------------------------------------


class TestFreshDependencySourceAcquire:
    """Tests for FreshDependencySource.acquire()."""

    def _make_source(
        self,
        ctx: MagicMock,
        dep_ref: MagicMock,
        install_path: Path,
        dep_key: str = "owner/repo",
        *,
        resolved_ref: Any = None,
        dep_locked_chk: Any = None,
        ref_changed: bool = False,
        progress: Any = None,
    ) -> Any:
        from apm_cli.install.sources import FreshDependencySource

        return FreshDependencySource(
            ctx,
            dep_ref,
            install_path,
            dep_key,
            resolved_ref,
            dep_locked_chk,
            ref_changed,
            progress,
        )

    def _mock_download_success(self, install_path: Path) -> MagicMock:
        """Return a package_info mock that looks like a successful download."""
        pkg_info = MagicMock()
        pkg_info.install_path = install_path
        pkg_info.package_type = None
        resolved = MagicMock()
        resolved.ref_name = "main"
        resolved.resolved_commit = "abc1234"
        pkg_info.resolved_reference = resolved
        return pkg_info

    def test_happy_path_no_logger_no_tui(self, tmp_path: Path) -> None:
        """Fresh download succeeds, no logger/tui, returns Materialization."""
        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="newhash"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is not None
        assert result.dep_key == "owner/repo"

    def test_happy_path_with_logger(self, tmp_path: Path) -> None:
        """Fresh download with logger → download_complete called."""
        mock_logger = MagicMock()
        ctx = _make_ctx(targets=["copilot"], logger=mock_logger, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="newhash"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        mock_logger.download_complete.assert_called_once()
        assert result is not None

    def test_happy_path_with_progress(self, tmp_path: Path) -> None:
        """Fresh download with progress object → add_task + update called."""
        mock_progress = MagicMock()
        mock_progress.add_task.return_value = 42
        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main", repo_url="o/r")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path, progress=mock_progress)
            result = source.acquire()

        mock_progress.add_task.assert_called_once()
        mock_progress.update.assert_called()
        assert result is not None

    def test_unpinned_dep_adds_delta(self, tmp_path: Path) -> None:
        """dep_ref.reference=None → deltas['unpinned']=1."""
        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference=None)
        dep_ref.reference = None
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is not None
        assert result.deltas.get("unpinned") == 1

    def test_hash_mismatch_raises_direct_dependency_error(self, tmp_path: Path) -> None:
        """Content hash mismatch returns to the transaction boundary."""
        from apm_cli.install.errors import DirectDependencyError

        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None, update_refs=False)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "sha"
        locked_chk.content_hash = "expected_hash"
        locked_chk.registry_prefix = None

        ctx.package_hashes["owner/repo"] = "actual_different_hash"

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="actual_different_hash",
            ),
            patch("apm_cli.utils.path_security.safe_rmtree"),
            patch("apm_cli.utils.console._rich_success"),
            pytest.raises(DirectDependencyError),
        ):
            source = self._make_source(ctx, dep_ref, install_path, dep_locked_chk=locked_chk)
            source.acquire()

    def test_exception_in_download_records_error_returns_none(self, tmp_path: Path) -> None:
        """Exception in download → diagnostics.error + None returned."""
        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(
                ctx.downloader, "download_package", side_effect=RuntimeError("network error")
            ),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is None
        ctx.diagnostics.error.assert_called_once()

    def test_no_targets_returns_none_package_info(self, tmp_path: Path) -> None:
        """ctx.targets=[] after download → Materialization with package_info=None."""
        ctx = _make_ctx(targets=[], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)
        pkg_info.package_type = None

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        assert result is not None
        assert result.package_info is None

    def test_pre_download_results_used_when_present(self, tmp_path: Path) -> None:
        """When dep_key in ctx.pre_download_results, skip downloader call."""
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)
        pkg_info.package_type = None

        ctx = _make_ctx(targets=["copilot"], logger=None, tui=None)
        ctx.pre_download_results["owner/repo"] = pkg_info

        dep_ref = _make_dep_ref(is_virtual=False, reference="main")

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            result = source.acquire()

        # download_package should NOT have been called since we used pre_download_results
        ctx.downloader.download_package.assert_not_called()
        assert result is not None

    def test_tui_task_start_and_complete_called(self, tmp_path: Path) -> None:
        """ctx.tui callbacks are invoked on start and complete."""
        mock_tui = MagicMock()
        ctx = _make_ctx(targets=["copilot"], logger=None, tui=mock_tui)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main", repo_url="owner/r")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            source = self._make_source(ctx, dep_ref, install_path)
            source.acquire()

        mock_tui.task_started.assert_called_once()
        mock_tui.task_completed.assert_called_once_with("owner/repo")


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
