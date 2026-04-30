"""Tests for ``apm_cli.install.sources`` -- DependencySource strategy classes.

Covers the factory function and acquire() paths that are not tested by
the existing ``test_sources_classification.py`` (which covers only
``_format_package_type_label``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.sources import (
    CachedDependencySource,
    FreshDependencySource,
    LocalDependencySource,
    Materialization,
    make_dependency_source,
)


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(
    *,
    is_local: bool = False,
    local_path: str = "",
    repo_url: str = "org/repo",
    reference: str = "main",
    is_virtual: bool = False,
    host: str = "github.com",
    port: Any = None,
) -> MagicMock:
    dep_ref = MagicMock()
    dep_ref.is_local = is_local
    dep_ref.local_path = local_path
    dep_ref.repo_url = repo_url
    dep_ref.reference = reference
    dep_ref.is_virtual = is_virtual
    dep_ref.host = host
    dep_ref.port = port
    return dep_ref


def _make_ctx(
    *,
    scope: Any = None,
    targets: list | None = None,
    logger: Any = None,
) -> MagicMock:
    """Build a minimal InstallContext mock."""
    ctx = MagicMock()
    ctx.scope = scope
    ctx.targets = targets if targets is not None else []
    ctx.logger = logger
    ctx.diagnostics = MagicMock()
    ctx.dependency_graph = MagicMock()
    ctx.dependency_graph.dependency_tree.get_node.return_value = None
    ctx.installed_packages = []
    ctx.package_hashes = {}
    ctx.package_types = {}
    ctx.callback_downloaded = {}
    ctx.existing_lockfile = None
    ctx.registry_config = None
    ctx.update_refs = False
    ctx.pre_download_results = {}
    ctx.auth_resolver = None
    ctx.apm_modules_dir = None
    ctx.project_root = Path("/tmp/project")
    return ctx


# ---------------------------------------------------------------------------
# Materialization dataclass
# ---------------------------------------------------------------------------


class TestMaterialization:
    def test_construction_with_defaults(self, tmp_path):
        mat = Materialization(
            package_info=None,
            install_path=tmp_path,
            dep_key="org/repo",
        )
        assert mat.package_info is None
        assert mat.install_path == tmp_path
        assert mat.dep_key == "org/repo"
        assert mat.deltas == {"installed": 1}

    def test_custom_deltas(self, tmp_path):
        mat = Materialization(
            package_info=None,
            install_path=tmp_path,
            dep_key="org/repo",
            deltas={"installed": 1, "unpinned": 1},
        )
        assert mat.deltas == {"installed": 1, "unpinned": 1}


# ---------------------------------------------------------------------------
# make_dependency_source factory
# ---------------------------------------------------------------------------


class TestMakeDependencySource:
    def test_local_dep_ref_returns_local_source(self, tmp_path):
        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        ctx = _make_ctx()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo", progress=MagicMock()
        )
        assert isinstance(src, LocalDependencySource)

    def test_skip_download_returns_cached_source(self, tmp_path):
        dep_ref = _make_dep_ref(is_local=False)
        ctx = _make_ctx()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo",
            skip_download=True, progress=MagicMock()
        )
        assert isinstance(src, CachedDependencySource)

    def test_fresh_download_returns_fresh_source(self, tmp_path):
        dep_ref = _make_dep_ref(is_local=False)
        ctx = _make_ctx()
        progress = MagicMock()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo",
            skip_download=False, progress=progress
        )
        assert isinstance(src, FreshDependencySource)

    def test_local_dep_takes_priority_over_skip_download(self, tmp_path):
        """A local dep ref returns LocalDependencySource even if
        skip_download=True, because the local check runs first."""
        dep_ref = _make_dep_ref(is_local=True, local_path="/pkg")
        ctx = _make_ctx()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo",
            skip_download=True, progress=MagicMock()
        )
        assert isinstance(src, LocalDependencySource)

    def test_resolved_ref_forwarded_to_cached_source(self, tmp_path):
        dep_ref = _make_dep_ref(is_local=False)
        ctx = _make_ctx()
        resolved = MagicMock()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo",
            skip_download=True, resolved_ref=resolved, progress=MagicMock()
        )
        assert isinstance(src, CachedDependencySource)
        assert src.resolved_ref is resolved

    def test_dep_locked_chk_forwarded_to_fresh_source(self, tmp_path):
        dep_ref = _make_dep_ref(is_local=False)
        ctx = _make_ctx()
        locked_chk = MagicMock()
        progress = MagicMock()
        src = make_dependency_source(
            ctx, dep_ref, tmp_path, "org/repo",
            dep_locked_chk=locked_chk, progress=progress
        )
        assert isinstance(src, FreshDependencySource)
        assert src.dep_locked_chk is locked_chk


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire
# ---------------------------------------------------------------------------


class TestLocalDependencySourceAcquire:
    def test_user_scope_returns_none_and_warns(self, tmp_path):
        """Local packages at user scope are skipped with a diagnostic."""
        from apm_cli.core.scope import InstallScope

        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        ctx = _make_ctx(scope=InstallScope.USER)
        src = LocalDependencySource(ctx, dep_ref, tmp_path, "local/pkg")

        result = src.acquire()

        assert result is None
        ctx.diagnostics.warn.assert_called_once()
        warn_args = ctx.diagnostics.warn.call_args[0][0]
        assert "local paths are not supported" in warn_args or "Skipped" in warn_args

    def test_user_scope_verbose_logger_called(self, tmp_path):
        """When a logger is provided, verbose detail is emitted on user-scope skip."""
        from apm_cli.core.scope import InstallScope

        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        logger = MagicMock()
        ctx = _make_ctx(scope=InstallScope.USER, logger=logger)
        src = LocalDependencySource(ctx, dep_ref, tmp_path, "local/pkg")

        result = src.acquire()

        assert result is None
        logger.verbose_detail.assert_called_once()

    def test_copy_failure_returns_none_and_records_error(self, tmp_path):
        """If _copy_local_package returns falsy, acquire returns None."""
        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        ctx = _make_ctx(scope=None)  # PROJECT scope (not USER)

        with patch(
            "apm_cli.install.sources.LocalDependencySource.acquire.__wrapped__"
            if hasattr(LocalDependencySource.acquire, "__wrapped__")
            else "apm_cli.install.phases.local_content._copy_local_package",
            return_value=None,
        ):
            # Patch at the import site within the method body
            with patch(
                "apm_cli.install.phases.local_content._copy_local_package",
                return_value=None,
            ):
                src = LocalDependencySource(ctx, dep_ref, tmp_path, "local/pkg")
                result = src.acquire()

        assert result is None
        ctx.diagnostics.error.assert_called_once()

    def test_successful_copy_with_apm_yml(self, tmp_path):
        """Successful local copy with apm.yml returns a Materialization."""
        from apm_cli.core.scope import InstallScope

        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        ctx = _make_ctx(scope=InstallScope.PROJECT)
        install_path = tmp_path / "install"
        install_path.mkdir()

        # Write a minimal apm.yml
        (install_path / "apm.yml").write_text("name: mypkg\nversion: 1.0.0\n")

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package",
            return_value=install_path,
        ), patch(
            "apm_cli.models.validation.detect_package_type",
            return_value=(MagicMock(), None),
        ), patch(
            "apm_cli.utils.content_hash.compute_package_hash",
            return_value="abc123",
        ):
            src = LocalDependencySource(ctx, dep_ref, install_path, "local/pkg")
            result = src.acquire()

        assert result is not None
        assert isinstance(result, Materialization)
        assert result.dep_key == "local/pkg"
        assert result.install_path == install_path

    def test_successful_copy_without_apm_yml(self, tmp_path):
        """When no apm.yml exists, a minimal APMPackage is synthesised."""
        from apm_cli.core.scope import InstallScope

        dep_ref = _make_dep_ref(is_local=True, local_path="/workspace/pkg")
        ctx = _make_ctx(scope=InstallScope.PROJECT)
        install_path = tmp_path / "install"
        install_path.mkdir()
        # No apm.yml

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package",
            return_value=install_path,
        ), patch(
            "apm_cli.models.validation.detect_package_type",
            return_value=(MagicMock(), None),
        ), patch(
            "apm_cli.utils.content_hash.compute_package_hash",
            return_value="abc123",
        ):
            src = LocalDependencySource(ctx, dep_ref, install_path, "local/pkg")
            result = src.acquire()

        assert result is not None
        assert isinstance(result, Materialization)


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire
# ---------------------------------------------------------------------------


class TestCachedDependencySourceAcquire:
    def test_no_targets_returns_early_materialization(self, tmp_path):
        """When ctx.targets is empty, integration is skipped (package_info=None)."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        install_path = tmp_path / "cache" / "org" / "repo"
        install_path.mkdir(parents=True)

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc123def456"
        locked_chk.registry_prefix = None

        src = CachedDependencySource(
            ctx, dep_ref, install_path, "org/repo",
            resolved_ref=None, dep_locked_chk=locked_chk,
        )
        result = src.acquire()

        assert result is not None
        assert result.package_info is None
        assert result.dep_key == "org/repo"

    def test_no_targets_with_unpinned_dep_sets_delta(self, tmp_path):
        """Unpinned deps (no reference) should set unpinned delta."""
        dep_ref = _make_dep_ref(reference="")  # no pin
        ctx = _make_ctx(targets=[])
        install_path = tmp_path / "cache" / "org" / "repo"
        install_path.mkdir(parents=True)

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc123def456"
        locked_chk.registry_prefix = None

        src = CachedDependencySource(
            ctx, dep_ref, install_path, "org/repo",
            resolved_ref=None, dep_locked_chk=locked_chk,
        )
        result = src.acquire()

        assert result is not None
        assert result.deltas.get("unpinned") == 1

    def test_pinned_dep_no_unpinned_delta(self, tmp_path):
        """Pinned deps must NOT have unpinned delta."""
        dep_ref = _make_dep_ref(reference="v1.2.3")
        ctx = _make_ctx(targets=[])
        install_path = tmp_path / "cache" / "org" / "repo"
        install_path.mkdir(parents=True)

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc123"
        locked_chk.registry_prefix = None

        src = CachedDependencySource(
            ctx, dep_ref, install_path, "org/repo",
            resolved_ref=None, dep_locked_chk=locked_chk,
        )
        result = src.acquire()

        assert "unpinned" not in result.deltas

    def test_with_targets_builds_package_info(self, tmp_path):
        """With targets, a full Materialization with PackageInfo is returned."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[MagicMock()])  # non-empty targets
        install_path = tmp_path / "cache" / "org" / "repo"
        install_path.mkdir(parents=True)
        (install_path / "apm.yml").write_text("name: repo\nversion: 0.1.0\n")

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc123def456"
        locked_chk.registry_prefix = None

        with patch(
            "apm_cli.models.validation.detect_package_type",
            return_value=(MagicMock(), None),
        ), patch(
            "apm_cli.utils.content_hash.compute_package_hash",
            return_value="hashval",
        ):
            src = CachedDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=locked_chk,
            )
            result = src.acquire()

        assert result is not None
        assert result.package_info is not None
        assert result.install_path == install_path

    def test_with_targets_no_apm_yml_builds_minimal_package(self, tmp_path):
        """Without apm.yml a synthetic APMPackage is used."""
        dep_ref = _make_dep_ref(reference="main", repo_url="org/myrepo")
        ctx = _make_ctx(targets=[MagicMock()])
        install_path = tmp_path / "cache"
        install_path.mkdir()
        # No apm.yml

        locked_chk = MagicMock()
        locked_chk.resolved_commit = None
        locked_chk.registry_prefix = None

        with patch(
            "apm_cli.models.validation.detect_package_type",
            return_value=(MagicMock(), None),
        ), patch(
            "apm_cli.utils.content_hash.compute_package_hash",
            return_value="hashval",
        ):
            src = CachedDependencySource(
                ctx, dep_ref, install_path, "org/myrepo",
                resolved_ref=None, dep_locked_chk=locked_chk,
            )
            result = src.acquire()

        assert result is not None
        assert result.package_info is not None
        # Synthetic package name is last segment of repo_url
        assert result.package_info.package.name == "myrepo"

    def test_logger_called_on_cache_hit(self, tmp_path):
        """download_complete is called with cached=True when a logger is present."""
        dep_ref = _make_dep_ref(reference="main")
        logger = MagicMock()
        ctx = _make_ctx(targets=[], logger=logger)
        install_path = tmp_path / "cache"
        install_path.mkdir()

        locked_chk = MagicMock()
        locked_chk.resolved_commit = "abc123def456"
        locked_chk.registry_prefix = None

        src = CachedDependencySource(
            ctx, dep_ref, install_path, "org/repo",
            resolved_ref=None, dep_locked_chk=locked_chk,
        )
        src.acquire()

        logger.download_complete.assert_called_once()
        call_kwargs = logger.download_complete.call_args
        # cached=True must be passed
        assert call_kwargs.kwargs.get("cached") is True or (
            len(call_kwargs.args) >= 4 and call_kwargs.args[3] is True
        )

    def test_locked_chk_none_is_handled(self, tmp_path):
        """dep_locked_chk=None should not raise."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        install_path = tmp_path
        install_path.mkdir(exist_ok=True)

        src = CachedDependencySource(
            ctx, dep_ref, install_path, "org/repo",
            resolved_ref=None, dep_locked_chk=None,
        )
        result = src.acquire()
        assert result is not None


# ---------------------------------------------------------------------------
# FreshDependencySource.acquire
# ---------------------------------------------------------------------------


class TestFreshDependencySourceAcquire:
    def _make_progress(self):
        progress = MagicMock()
        progress.add_task.return_value = 1
        return progress

    def test_download_exception_returns_none_and_records_error(self, tmp_path):
        """Any exception during download returns None and records a diagnostics error."""
        dep_ref = _make_dep_ref()
        ctx = _make_ctx()
        progress = self._make_progress()

        with patch("apm_cli.drift.build_download_ref", side_effect=RuntimeError("network error")):
            src = FreshDependencySource(
                ctx, dep_ref, tmp_path, "org/repo",
                resolved_ref=None, dep_locked_chk=None,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is None
        ctx.diagnostics.error.assert_called_once()
        err_msg = ctx.diagnostics.error.call_args[0][0]
        assert "org/repo" in err_msg or "Failed to install" in err_msg

    def test_successful_download_no_targets_returns_materialization_no_info(self, tmp_path):
        """Without targets, package_info is None in the returned Materialization."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "deadbeef"
        fake_pkg_info.resolved_reference.ref_name = "main"
        fake_pkg_info.package_type = None

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h1"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=None,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is not None
        assert result.package_info is None
        assert result.dep_key == "org/repo"

    def test_successful_download_with_targets_returns_package_info(self, tmp_path):
        """With targets present, the downloaded package_info is returned."""
        dep_ref = _make_dep_ref(reference="v1.0")
        ctx = _make_ctx(targets=[MagicMock()])
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "deadbeef"
        fake_pkg_info.resolved_reference.ref_name = "v1.0"
        fake_pkg_info.package_type = None

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h1"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=None,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is not None
        assert result.package_info is fake_pkg_info

    def test_hash_mismatch_calls_sys_exit(self, tmp_path):
        """Content hash mismatch must abort via sys.exit(1)."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        ctx.update_refs = False
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "deadbeef"
        fake_pkg_info.resolved_reference.ref_name = "main"
        fake_pkg_info.package_type = None

        locked_chk = MagicMock()
        locked_chk.content_hash = "expected_hash"

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="different_hash"), \
             patch("apm_cli.utils.path_security.safe_rmtree"), \
             patch("apm_cli.install.sources._rich_error"), \
             pytest.raises(SystemExit) as exc_info:
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=locked_chk,
                ref_changed=False, progress=progress,
            )
            src.acquire()

        assert exc_info.value.code == 1

    def test_hash_match_does_not_exit(self, tmp_path):
        """Matching content hash allows download to proceed normally."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        ctx.update_refs = False
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "deadbeef"
        fake_pkg_info.resolved_reference.ref_name = "main"
        fake_pkg_info.package_type = None

        locked_chk = MagicMock()
        locked_chk.content_hash = "same_hash"

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="same_hash"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=locked_chk,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is not None  # did not sys.exit

    def test_update_refs_skips_hash_verification(self, tmp_path):
        """When update_refs=True, hash mismatch must NOT abort."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        ctx.update_refs = True  # update mode
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "deadbeef"
        fake_pkg_info.resolved_reference.ref_name = "main"
        fake_pkg_info.package_type = None

        locked_chk = MagicMock()
        locked_chk.content_hash = "expected_hash"

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="different_hash"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=locked_chk,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is not None  # no sys.exit

    def test_unpinned_dep_sets_unpinned_delta(self, tmp_path):
        """Downloads without a pinned reference record unpinned delta."""
        dep_ref = _make_dep_ref(reference="")  # no pin
        ctx = _make_ctx(targets=[])
        progress = self._make_progress()
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "abc"
        fake_pkg_info.resolved_reference.ref_name = ""
        fake_pkg_info.package_type = None

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch.object(ctx.downloader, "download_package", return_value=fake_pkg_info), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=None,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        assert result is not None
        assert result.deltas.get("unpinned") == 1

    def test_pre_downloaded_result_used_when_available(self, tmp_path):
        """When dep_key is in ctx.pre_download_results, the downloader is not called."""
        dep_ref = _make_dep_ref(reference="main")
        ctx = _make_ctx(targets=[])
        install_path = tmp_path / "install"
        install_path.mkdir()

        fake_pkg_info = MagicMock()
        fake_pkg_info.install_path = install_path
        fake_pkg_info.resolved_reference.resolved_commit = "pre_dl"
        fake_pkg_info.resolved_reference.ref_name = "main"
        fake_pkg_info.package_type = None
        ctx.pre_download_results["org/repo"] = fake_pkg_info

        progress = self._make_progress()

        with patch("apm_cli.drift.build_download_ref", return_value=MagicMock()), \
             patch("apm_cli.utils.content_hash.compute_package_hash", return_value="h"):
            src = FreshDependencySource(
                ctx, dep_ref, install_path, "org/repo",
                resolved_ref=None, dep_locked_chk=None,
                ref_changed=False, progress=progress,
            )
            result = src.acquire()

        # downloader.download_package must NOT have been called
        ctx.downloader.download_package.assert_not_called()
        assert result is not None


# ---------------------------------------------------------------------------
# Integration error prefix constants
# ---------------------------------------------------------------------------


class TestIntegrateErrorPrefix:
    def test_local_source_custom_prefix(self, tmp_path):
        ctx = _make_ctx()
        dep_ref = _make_dep_ref(is_local=True, local_path="/p")
        src = LocalDependencySource(ctx, dep_ref, tmp_path, "key")
        assert "local package" in src.INTEGRATE_ERROR_PREFIX

    def test_cached_source_custom_prefix(self, tmp_path):
        ctx = _make_ctx()
        dep_ref = _make_dep_ref()
        src = CachedDependencySource(
            ctx, dep_ref, tmp_path, "key", resolved_ref=None, dep_locked_chk=None
        )
        assert "cached package" in src.INTEGRATE_ERROR_PREFIX

    def test_fresh_source_default_prefix(self, tmp_path):
        ctx = _make_ctx()
        dep_ref = _make_dep_ref()
        progress = MagicMock()
        src = FreshDependencySource(
            ctx, dep_ref, tmp_path, "key",
            resolved_ref=None, dep_locked_chk=None,
            ref_changed=False, progress=progress,
        )
        assert "Failed to integrate primitives" in src.INTEGRATE_ERROR_PREFIX
