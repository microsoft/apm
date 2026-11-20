"""Unit tests for apm_cli.install.sources.

Covers the DependencySource strategy classes and their helpers that are not
yet reached by test_sources_classification.py:

* Materialization dataclass - field defaults and custom deltas
* LocalDependencySource.acquire
  - USER-scope + relative path → skip + return None
  - USER-scope + absolute path → allowed
  - _copy_local_package failure → return None
  - success path with apm.yml present
  - fail-closed path without a supported manifest
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
  - resolved_ref=None branch (fallback carries only a vouched full-SHA source pin)
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

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component

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

    ctx.dep_base_dirs = {}
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

    def test_without_supported_manifest_fails_closed(self, tmp_path: Path) -> None:
        """A copied local directory without a package contract is rejected."""
        from apm_cli.install.errors import DirectDependencyError

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
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc123",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            with pytest.raises(DirectDependencyError, match="Local package is invalid"):
                source.acquire()

        assert not (install_path / "apm.yml").exists()

    def test_invalid_apm_path_does_not_enter_legacy_fallback(self, tmp_path: Path) -> None:
        """A malformed .apm path remains invalid despite a root apm.yml."""
        from apm_cli.install.errors import DirectDependencyError
        from apm_cli.install.sources import LocalDependencySource

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "apm.yml").write_text("name: invalid\nversion: 1.0.0\n")
        (install_path / ".apm").write_text("not a directory\n")

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package",
            return_value=install_path,
        ):
            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            with pytest.raises(DirectDependencyError, match=r"\.apm must be a directory"):
                source.acquire()

    def test_success_with_apm_yml(self, tmp_path: Path) -> None:
        """Success path: apm.yml present → APMPackage.from_apm_yml called."""
        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(
            is_local=True, local_path=str(tmp_path / "src_pkg"), reference="local"
        )
        install_path = tmp_path / "install"
        install_path.mkdir()

        # Create a minimal apm.yml in the install path
        (install_path / "apm.yml").write_text(
            "name: mypkg\nversion: 0.1.0\ndependencies:\n  apm:\n    - owner/repo\n"
        )

        mock_pkg = MagicMock()
        mock_pkg.source = None

        from apm_cli.utils.content_hash import PackageDigests

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
                "apm_cli.utils.content_hash.compute_package_digests",
                return_value=PackageDigests("deadbeef", "deadbeef"),
            ) as hash_package,
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            result = source.acquire()

        assert result is not None
        assert ctx.package_hashes["owner/pkg"] == "deadbeef"
        hash_package.assert_called_once_with(install_path, exclude=(), legacy=False)

    def test_logger_download_complete_called_on_success(self, tmp_path: Path) -> None:
        """logger.download_complete should be called on the happy path."""
        mock_logger = MagicMock()
        ctx = _make_ctx(project_root=tmp_path, logger=mock_logger)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "apm.yml").write_text(
            "name: mypkg\nversion: 0.1.0\ndependencies:\n  apm:\n    - owner/repo\n"
        )

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

    def test_marketplace_plugin_normalizes_through_validation(self, tmp_path: Path) -> None:
        """Legacy marketplace plugins retain their explicit normalization path."""
        from apm_cli.models.apm_package import PackageType

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        fake_plugin_json = install_path / "plugin.json"
        fake_plugin_json.write_text('{"name": "legacy-plugin"}', encoding="utf-8")

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/plugin")
            result = source.acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.package_info.package_type == PackageType.MARKETPLACE_PLUGIN
        assert (install_path / "apm.yml").exists()

    def test_logger_download_complete_called_for_skill_package(self, tmp_path: Path) -> None:
        """logger.download_complete should be called on the happy path."""
        mock_logger = MagicMock()
        ctx = _make_ctx(project_root=tmp_path, logger=mock_logger)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "SKILL.md").write_text(
            "---\nname: logger-skill\ndescription: Logger skill\n---\n"
        )

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
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
        """Legacy MARKETPLACE_PLUGIN still normalizes through validation."""
        from apm_cli.models.apm_package import PackageType

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(tmp_path), reference=None)
        install_path = tmp_path / "install"
        install_path.mkdir()
        fake_plugin_json = install_path / "plugin.json"
        fake_plugin_json.write_text('{"name":"legacy-plugin"}', encoding="utf-8")

        with (
            patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=install_path,
            ),
            patch(
                "apm_cli.utils.content_hash.compute_package_hash",
                return_value="abc",
            ),
        ):
            from apm_cli.install.sources import LocalDependencySource

            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/plugin")
            result = source.acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.package_info.package_type == PackageType.MARKETPLACE_PLUGIN
        assert (install_path / "apm.yml").exists()

    def test_malformed_native_manifest_fails_before_copy(self, tmp_path: Path) -> None:
        """Exact-root Agent Plugin errors fail before the install target mutates."""
        from apm_cli.install.errors import DirectDependencyError
        from apm_cli.install.sources import LocalDependencySource

        source_root = tmp_path / "source"
        source_root.mkdir()
        (source_root / "plugin.json").write_text("{bad json", encoding="utf-8")
        install_path = tmp_path / "apm_modules" / "_local" / "native"
        install_path.mkdir(parents=True)
        sentinel = install_path / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(is_local=True, local_path=str(source_root), reference=None)

        with patch("apm_cli.install.phases.local_content._copy_local_package") as copy:
            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/native")
            with pytest.raises(DirectDependencyError, match="Local Agent Plugin is invalid"):
                source.acquire()

        copy.assert_not_called()
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        assert not (source_root / "apm.yml").exists()


# ---------------------------------------------------------------------------
# CachedDependencySource._resolve_cached_commit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire
# ---------------------------------------------------------------------------


_LOCKED_SHA = "f1b2ba49d18a3de11f87d8e6dc672dfdaadbff85"


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

    def test_no_targets_preserves_native_package_info_for_boundary(self, tmp_path: Path) -> None:
        from apm_cli.agent_plugins import PLUGIN_SCHEMA_ID
        from apm_cli.models.validation import PackageType

        ctx = _make_ctx(targets=[])
        ctx.lockfile_only = False
        ctx.git_semver_resolutions = {}
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        dep_ref.source = "git"
        install_path = tmp_path / "native"
        install_path.mkdir()
        (install_path / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": PLUGIN_SCHEMA_ID,
                    "name": "native.plugin",
                    "version": "1.0.0",
                }
            ),
            encoding="utf-8",
        )

        with patch(
            "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
            return_value="abc",
        ):
            result = self._make_source(ctx, dep_ref, install_path).acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.package_info.package_type is PackageType.AGENT_PLUGIN

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
        assert not (install_path / "apm.yml").exists()

    @pytest.mark.parametrize(
        ("cached_commit", "marker_commit", "fetched_this_run", "expected"),
        [
            pytest.param(_LOCKED_SHA, _LOCKED_SHA, False, _LOCKED_SHA, id="locked-sha-and-pin"),
            pytest.param(_LOCKED_SHA, None, True, _LOCKED_SHA, id="fetched-this-run"),
            pytest.param(_LOCKED_SHA, None, False, "cached", id="missing-pin"),
            pytest.param(_LOCKED_SHA, "a" * 40, False, "cached", id="inconsistent-pin"),
            pytest.param("f1b2ba4", "f1b2ba4", False, "cached", id="short-sha"),
            pytest.param("main", "main", False, "cached", id="branch-name"),
            pytest.param(None, None, False, "cached", id="no-commit"),
        ],
    )
    def test_resolved_ref_none_fallback_carries_only_a_vouched_source_pin(
        self,
        tmp_path: Path,
        cached_commit: str | None,
        marker_commit: str | None,
        fetched_this_run: bool,
        expected: str,
    ) -> None:
        """Only a full commit the cache vouches for replaces the sentinel (#3388).

        Lockfile bookkeeping keeps the resolved cached commit in every case.
        """
        from apm_cli.install.cache_pin import write_marker

        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, reference="feature-branch")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "apm.yml").write_text("name: test\nversion: 0.0.1\n")
        if marker_commit is not None:
            write_marker(install_path, marker_commit)

        mock_pkg = MagicMock()
        mock_pkg.source = "owner/repo"

        with (
            patch("apm_cli.models.apm_package.APMPackage.from_apm_yml", return_value=mock_pkg),
            patch("apm_cli.models.validation.detect_package_type", return_value=(None, None)),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"),
            patch(
                "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
                return_value=cached_commit,
            ),
        ):
            source = self._make_source(
                ctx,
                dep_ref,
                install_path,
                resolved_ref=None,
                fetched_this_run=fetched_this_run,
            )
            result = source.acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.package_info.resolved_reference.resolved_commit == expected
        assert ctx.installed_packages[-1].resolved_commit == cached_commit

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

    def test_no_targets_still_rejects_malformed_native_manifest(self, tmp_path: Path) -> None:
        """Native classification runs before the cached no-target shortcut."""
        from apm_cli.install.errors import DirectDependencyError

        ctx = _make_ctx(targets=[])
        dep_ref = _make_dep_ref(is_virtual=False)
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        plugin_json = install_path / "plugin.json"
        plugin_json.write_text("{bad json", encoding="utf-8")

        source = self._make_source(ctx, dep_ref, install_path)
        with pytest.raises(DirectDependencyError, match="Cached Agent Plugin is invalid"):
            source.acquire()

        assert plugin_json.read_text(encoding="utf-8") == "{bad json"
        assert not (install_path / "apm.yml").exists()

    def test_receiptless_plugin_error_identifies_cache_and_recovery(self, tmp_path: Path) -> None:
        """Cached acquisition names invalid legacy state and its recovery action."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.install.errors import DirectDependencyError
        from apm_cli.utils.content_hash import compute_package_hash

        install_path = tmp_path / "cached-plugin"
        skill = install_path / "skills" / "valid"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# valid\n", encoding="ascii")
        (install_path / ".apm" / "skills" / "valid").mkdir(parents=True)
        (install_path / ".apm" / "skills" / "valid" / "SKILL.md").write_text(
            "# valid\n", encoding="ascii"
        )
        (install_path / "apm.yml").write_text(
            "name: cached-plugin\nversion: 0.1.0\n", encoding="ascii"
        )
        (install_path / "plugin.json").write_text(
            '{"name":"cached-plugin","skills":["./missing-skills/"]}',
            encoding="ascii",
        )
        locked_dep = LockedDependency(
            repo_url="owner/cached-plugin",
            package_type="marketplace_plugin",
            content_hash=compute_package_hash(install_path),
        )
        existing_lockfile = LockFile(apm_version="0.28.0")
        existing_lockfile.add_dependency(locked_dep)
        ctx = _make_ctx(
            targets=["claude"],
            existing_lockfile=existing_lockfile,
        )
        source = self._make_source(
            ctx,
            _make_dep_ref(repo_url="owner/cached-plugin"),
            install_path,
            dep_key="owner/cached-plugin",
            dep_locked_chk=locked_dep,
        )

        with pytest.raises(DirectDependencyError) as exc_info:
            source.acquire()

        message = str(exc_info.value)
        assert "owner/cached-plugin" in message
        assert str(install_path) in message
        assert "apm deps clean --yes" in message

    @pytest.mark.parametrize(
        ("apm_version", "package_type", "fetched_this_run"),
        [
            ("0.29.0", "marketplace_plugin", False),
            ("0.28.0", "apm_package", False),
            ("0.28.0", "marketplace_plugin", True),
        ],
    )
    def test_receiptless_plugin_upgrade_requires_verified_legacy_lock(
        self,
        tmp_path: Path,
        apm_version: str,
        package_type: str,
        fetched_this_run: bool,
    ) -> None:
        """Only verified, previously locked 0.28 plugin caches may be repaired."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.utils.content_hash import compute_package_hash

        install_path = tmp_path / "cached-plugin"
        (install_path / ".apm").mkdir(parents=True)
        (install_path / "apm.yml").write_text(
            "name: cached-plugin\nversion: 0.1.0\n", encoding="ascii"
        )
        (install_path / "plugin.json").write_text('{"name":"cached-plugin"}', encoding="ascii")
        locked_dep = LockedDependency(
            repo_url="owner/cached-plugin",
            package_type=package_type,
            content_hash=compute_package_hash(install_path),
        )
        existing_lockfile = LockFile(apm_version=apm_version)
        existing_lockfile.add_dependency(locked_dep)
        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        cached_package = MagicMock(source="owner/cached-plugin")

        with (
            patch(
                "apm_cli.install.legacy_plugin_compat.validate_legacy_marketplace_plugin"
            ) as validate,
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                return_value=cached_package,
            ),
        ):
            source = self._make_source(
                ctx,
                _make_dep_ref(repo_url="owner/cached-plugin"),
                install_path,
                dep_key="owner/cached-plugin",
                dep_locked_chk=locked_dep,
                fetched_this_run=fetched_this_run,
            )
            result = source.acquire()

        assert result is not None
        validate.assert_not_called()

    def test_acquire_hands_fetched_state_to_legacy_upgrade(self, tmp_path: Path) -> None:
        """Cached acquisition explicitly excludes bytes fetched in this run."""
        from apm_cli.deps.lockfile import LockedDependency

        install_path = tmp_path / "cached-plugin"
        install_path.mkdir()
        (install_path / "apm.yml").write_text(
            "name: cached-plugin\nversion: 0.1.0\n", encoding="ascii"
        )
        locked_dep = LockedDependency(
            repo_url="owner/cached-plugin",
            package_type="marketplace_plugin",
            content_hash="sha256:locked",
        )
        existing_lockfile = MagicMock(apm_version="0.28.0")
        existing_lockfile.get_dependency.return_value = locked_dep
        ctx = _make_ctx(existing_lockfile=existing_lockfile)
        cached_package = MagicMock(source="owner/cached-plugin")

        with (
            patch(
                "apm_cli.install.legacy_plugin_compat.upgrade_cached_legacy_plugin",
                return_value=None,
            ) as upgrade,
            patch(
                "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                return_value=cached_package,
            ),
        ):
            source = self._make_source(
                ctx,
                _make_dep_ref(repo_url="owner/cached-plugin"),
                install_path,
                dep_key="owner/cached-plugin",
                dep_locked_chk=locked_dep,
                fetched_this_run=True,
            )
            result = source.acquire()

        assert result is not None
        upgrade.assert_called_once_with(
            install_path,
            "owner/cached-plugin",
            lockfile=existing_lockfile,
            fetched_this_run=True,
        )

    def test_cached_marketplace_plugin_is_not_normalized_or_mutated(self, tmp_path: Path) -> None:
        """Legacy cached marketplace plugins retain their prior read-only fallback."""
        from apm_cli.models.validation import PackageType

        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, repo_url="owner/plugin", reference="main")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        plugin_json = install_path / "plugin.json"
        plugin_json.write_text('{"name": "legacy-plugin"}', encoding="utf-8")
        before = plugin_json.read_bytes()

        with patch(
            "apm_cli.install.sources.CachedDependencySource._resolve_cached_commit",
            return_value="commit",
        ):
            result = self._make_source(ctx, dep_ref, install_path).acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.package_info.package_type == PackageType.MARKETPLACE_PLUGIN
        assert plugin_json.read_bytes() == before
        assert not (install_path / "apm.yml").exists()
        assert not (install_path / ".apm").exists()

    def test_resolved_ref_none_creates_fallback_resolved_reference(self, tmp_path: Path) -> None:
        """resolved_ref=None → a synthetic ResolvedReference is built."""
        ctx = _make_ctx(targets=["copilot"])
        dep_ref = _make_dep_ref(is_virtual=False, reference="feature-branch")
        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "apm.yml").write_text(
            "name: test\nversion: 0.0.1\ndependencies:\n  apm:\n    - owner/repo\n"
        )

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

    def test_no_targets_preserves_native_package_info_for_boundary(self, tmp_path: Path) -> None:
        from apm_cli.models.validation import PackageType

        ctx = _make_ctx(targets=[], logger=None, tui=None)
        dep_ref = _make_dep_ref(is_virtual=False, reference="main")
        dep_ref.source = "git"
        install_path = tmp_path / "native"
        install_path.mkdir()
        pkg_info = self._mock_download_success(install_path)
        pkg_info.package_type = PackageType.AGENT_PLUGIN
        pkg_info.package = MagicMock(name="native.plugin", version="1.0.0")

        with (
            patch("apm_cli.drift.build_download_ref", return_value=MagicMock()),
            patch.object(ctx.downloader, "download_package", return_value=pkg_info),
            patch("apm_cli.utils.content_hash.compute_package_hash", return_value="hash"),
            patch("apm_cli.utils.console._rich_success"),
        ):
            result = self._make_source(ctx, dep_ref, install_path).acquire()

        assert result is not None
        assert result.package_info is pkg_info

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
            pytest.raises(DirectDependencyError, match="Content hash mismatch"),
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
