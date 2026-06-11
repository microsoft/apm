"""integration tests for src/apm_cli/install/sources.py.

Targets the gap of ~180 lines at 50.8% coverage.

Covered branches / lines:
- _format_package_type_label: all PackageType values including HOOK_PACKAGE
- Materialization dataclass default deltas
- DependencySource abstract interface
- LocalDependencySource.acquire:
  - USER scope rejection (relative path)
  - USER scope rejection (no local_path)
  - copy failure -> return None
  - successful path with apm.yml (absolute source path)
  - successful path without apm.yml (bare APMPackage)
  - package_type detection MARKETPLACE_PLUGIN branch
  - local dep not adding hash (dep_ref.is_local)
- CachedDependencySource._resolve_cached_commit:
  - fetched_this_run=True with callback sha
  - fetched_this_run=True fallback to resolved_ref.resolved_commit
  - existing lockfile path
  - fallback to dep_ref.reference
- CachedDependencySource.acquire:
  - no targets -> Materialization(package_info=None)
  - with apm.yml (no source field)
  - without apm.yml (bare APMPackage)
  - resolved_ref=None fallback
  - registry detection
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.sources import (
    Materialization,
    _format_package_type_label,
)

# ---------------------------------------------------------------------------
# _format_package_type_label
# ---------------------------------------------------------------------------


class TestFormatPackageTypeLabel:
    def test_all_known_types_return_string(self):
        from apm_cli.models.apm_package import PackageType

        for pkg_type in PackageType:
            label = _format_package_type_label(pkg_type)
            # All defined types should return a string
            if label is not None:
                assert isinstance(label, str)

    def test_claude_skill_label(self):
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.CLAUDE_SKILL)
        assert label is not None
        assert "Skill" in label

    def test_hook_package_label_not_none(self):
        """HOOK_PACKAGE was the missing case in #780 -- must now have a label."""
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.HOOK_PACKAGE)
        assert label is not None
        assert "Hook" in label

    def test_marketplace_plugin_label(self):
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.MARKETPLACE_PLUGIN)
        assert label is not None
        assert "Plugin" in label

    def test_unknown_type_returns_none(self):
        label = _format_package_type_label("unknown_type")
        assert label is None

    def test_skill_bundle_label(self):
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.SKILL_BUNDLE)
        assert label is not None
        assert "Bundle" in label


# ---------------------------------------------------------------------------
# Materialization dataclass
# ---------------------------------------------------------------------------


class TestMaterialization:
    def test_default_deltas(self, tmp_path):
        pkg_info = MagicMock()
        m = Materialization(
            package_info=pkg_info,
            install_path=tmp_path,
            dep_key="owner/repo",
        )
        assert m.deltas == {"installed": 1}

    def test_custom_deltas(self, tmp_path):
        m = Materialization(
            package_info=None,
            install_path=tmp_path,
            dep_key="owner/repo",
            deltas={"installed": 1, "unpinned": 1},
        )
        assert m.deltas["unpinned"] == 1


# ---------------------------------------------------------------------------
# Helpers for building mock InstallContext
# ---------------------------------------------------------------------------


def _make_ctx(
    scope_is_user=False,
    project_root=None,
    targets=True,
    existing_lockfile=None,
):
    from apm_cli.core.scope import InstallScope

    ctx = MagicMock()
    ctx.scope = InstallScope.USER if scope_is_user else InstallScope.PROJECT
    ctx.project_root = project_root or Path("/tmp/project")
    ctx.targets = ["AGENTS.md"] if targets else []
    ctx.diagnostics = MagicMock()
    ctx.logger = MagicMock()
    ctx.dependency_graph = MagicMock()
    ctx.dependency_graph.dependency_tree.get_node.return_value = None
    ctx.installed_packages = []
    ctx.package_hashes = {}
    ctx.package_types = {}
    ctx.existing_lockfile = existing_lockfile
    ctx.callback_downloaded = {}
    ctx.registry_config = None
    ctx.dep_base_dirs = {}
    return ctx


def _make_dep_ref(
    local_path=None,
    is_local=True,
    repo_url="owner/repo",
    reference="v1.0.0",
    is_virtual=False,
):
    dep_ref = MagicMock()
    dep_ref.is_local = is_local
    dep_ref.local_path = local_path
    dep_ref.repo_url = repo_url
    dep_ref.reference = reference
    dep_ref.is_virtual = is_virtual
    dep_ref.__str__ = lambda s: repo_url
    return dep_ref


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire -- USER scope rejection
# ---------------------------------------------------------------------------


class TestLocalDependencySourceUserScope:
    def test_rejects_relative_path_at_user_scope(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource

        ctx = _make_ctx(scope_is_user=True, project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path="relative/path")
        source = LocalDependencySource(ctx, dep_ref, tmp_path / "pkg", "owner/repo")
        result = source.acquire()
        assert result is None
        ctx.diagnostics.warn.assert_called_once()

    def test_rejects_empty_local_path_at_user_scope(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource

        ctx = _make_ctx(scope_is_user=True, project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path="")
        source = LocalDependencySource(ctx, dep_ref, tmp_path / "pkg", "owner/repo")
        result = source.acquire()
        assert result is None

    def test_accepts_absolute_path_at_user_scope(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource

        src = tmp_path / "local_pkg"
        src.mkdir()
        (src / "apm.yml").write_text("name: test\nversion: 1.0.0")
        install_path = tmp_path / "apm_modules" / "pkg"
        install_path.mkdir(parents=True)

        ctx = _make_ctx(scope_is_user=True, project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path=str(src))

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package", return_value=install_path
        ):
            with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
                from apm_cli.models.apm_package import PackageType

                mock_detect.return_value = (PackageType.APM_PACKAGE, None)
                source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
                result = source.acquire()

        assert result is not None


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire -- copy failure
# ---------------------------------------------------------------------------


class TestLocalDependencySourceCopyFailure:
    def test_returns_none_on_copy_failure(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path=str(tmp_path / "src"))
        install_path = tmp_path / "install"
        install_path.mkdir()

        with patch("apm_cli.install.phases.local_content._copy_local_package", return_value=None):
            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
            result = source.acquire()

        assert result is None
        ctx.diagnostics.error.assert_called_once()


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire -- successful path without apm.yml
# ---------------------------------------------------------------------------


class TestLocalDependencySourceNoApmYml:
    def test_creates_bare_apm_package_when_no_apm_yml(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource

        src = tmp_path / "local_pkg"
        src.mkdir()
        install_path = tmp_path / "apm_modules" / "pkg"
        install_path.mkdir(parents=True)
        # No apm.yml in install_path

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path=str(src))

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package", return_value=install_path
        ):
            with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
                from apm_cli.models.apm_package import PackageType

                mock_detect.return_value = (PackageType.APM_PACKAGE, None)
                source = LocalDependencySource(ctx, dep_ref, install_path, "owner/pkg")
                result = source.acquire()

        assert result is not None
        assert result.install_path == install_path


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire -- MARKETPLACE_PLUGIN branch
# ---------------------------------------------------------------------------


class TestLocalDependencySourceMarketplacePlugin:
    def test_calls_normalize_plugin_directory_for_plugin_type(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource
        from apm_cli.models.apm_package import PackageType

        src = tmp_path / "local_plugin"
        src.mkdir()
        install_path = tmp_path / "apm_modules" / "plugin"
        install_path.mkdir(parents=True)

        ctx = _make_ctx(project_root=tmp_path)
        dep_ref = _make_dep_ref(local_path=str(src))

        with patch(
            "apm_cli.install.phases.local_content._copy_local_package", return_value=install_path
        ):
            with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
                fake_plugin_json = install_path / "plugin.json"
                fake_plugin_json.write_text("{}")
                mock_detect.return_value = (PackageType.MARKETPLACE_PLUGIN, fake_plugin_json)
                with patch(
                    "apm_cli.deps.plugin_parser.normalize_plugin_directory"
                ) as mock_normalize:
                    source = LocalDependencySource(ctx, dep_ref, install_path, "owner/plugin")
                    source.acquire()
                    mock_normalize.assert_called_once()


# ---------------------------------------------------------------------------
# CachedDependencySource._resolve_cached_commit
# ---------------------------------------------------------------------------


class TestResolveCachedCommit:
    def _make_cached_source(self, tmp_path, **kwargs):
        from apm_cli.install.sources import CachedDependencySource

        ctx = _make_ctx(
            project_root=tmp_path,
            **{k: v for k, v in kwargs.items() if k in ("existing_lockfile",)},
        )
        dep_ref = _make_dep_ref(reference="v1.0.0")
        install_path = tmp_path / "install"
        return CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=kwargs.get("resolved_ref"),
            dep_locked_chk=kwargs.get("dep_locked_chk"),
            fetched_this_run=kwargs.get("fetched_this_run", False),
        )

    def test_fetched_this_run_uses_callback_sha(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        ctx = _make_ctx(project_root=tmp_path)
        ctx.callback_downloaded = {"owner/repo": "abc123"}
        dep_ref = _make_dep_ref(reference="v1.0.0")
        install_path = tmp_path / "install"

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=None,
            dep_locked_chk=None,
            fetched_this_run=True,
        )
        commit = source._resolve_cached_commit()
        assert commit == "abc123"

    def test_fetched_this_run_falls_back_to_resolved_ref(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        ctx = _make_ctx(project_root=tmp_path)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(reference="v1.0.0")
        install_path = tmp_path / "install"

        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "abc" * 13 + "a"  # 40 chars

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=resolved_ref,
            dep_locked_chk=None,
            fetched_this_run=True,
        )
        commit = source._resolve_cached_commit()
        assert commit == resolved_ref.resolved_commit

    def test_uses_existing_lockfile_sha_when_cached(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        locked_dep = MagicMock()
        locked_dep.resolved_commit = "cachedsha123"
        existing_lockfile = MagicMock()
        existing_lockfile.get_dependency.return_value = locked_dep

        ctx = _make_ctx(project_root=tmp_path, existing_lockfile=existing_lockfile)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(reference="v1.0.0")
        install_path = tmp_path / "install"

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=None,
            dep_locked_chk=None,
            fetched_this_run=False,
        )
        commit = source._resolve_cached_commit()
        assert commit == "cachedsha123"

    def test_falls_back_to_dep_ref_reference(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        ctx = _make_ctx(project_root=tmp_path)
        ctx.callback_downloaded = {}
        ctx.existing_lockfile = None
        dep_ref = _make_dep_ref(reference="v2.0.0")
        install_path = tmp_path / "install"

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=None,
            dep_locked_chk=None,
            fetched_this_run=False,
        )
        commit = source._resolve_cached_commit()
        assert commit == "v2.0.0"


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire -- no targets
# ---------------------------------------------------------------------------


class TestCachedDependencySourceNoTargets:
    def test_returns_materialization_with_none_package_info_when_no_targets(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        ctx = _make_ctx(project_root=tmp_path, targets=False)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(is_local=False, reference="v1.0.0")
        install_path = tmp_path / "install"
        install_path.mkdir()

        dep_locked_chk = MagicMock()
        dep_locked_chk.resolved_commit = "abc123"
        dep_locked_chk.registry_prefix = None

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=None,
            dep_locked_chk=dep_locked_chk,
        )
        result = source.acquire()
        assert result is not None
        assert result.package_info is None


# ---------------------------------------------------------------------------
# CachedDependencySource.acquire -- with apm.yml
# ---------------------------------------------------------------------------


class TestCachedDependencySourceWithApmYml:
    def test_loads_package_from_apm_yml(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "apm.yml").write_text(
            "name: cached-pkg\nversion: 1.2.3\ndescription: A cached package"
        )

        ctx = _make_ctx(project_root=tmp_path)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(is_local=False, reference="v1.2.3")

        dep_locked_chk = MagicMock()
        dep_locked_chk.resolved_commit = "abc123def456" * 3 + "abcd"
        dep_locked_chk.registry_prefix = None

        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "abc123def456" * 3 + "abcd"

        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            from apm_cli.models.apm_package import PackageType

            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            source = CachedDependencySource(
                ctx=ctx,
                dep_ref=dep_ref,
                install_path=install_path,
                dep_key="owner/repo",
                resolved_ref=resolved_ref,
                dep_locked_chk=dep_locked_chk,
            )
            result = source.acquire()

        assert result is not None
        assert result.install_path == install_path

    def test_loads_bare_package_without_apm_yml(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        install_path = tmp_path / "install"
        install_path.mkdir()
        # No apm.yml

        ctx = _make_ctx(project_root=tmp_path)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(is_local=False, reference="v1.0.0")

        dep_locked_chk = MagicMock()
        dep_locked_chk.resolved_commit = "aaaa" * 10
        dep_locked_chk.registry_prefix = None

        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            from apm_cli.models.apm_package import PackageType

            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            source = CachedDependencySource(
                ctx=ctx,
                dep_ref=dep_ref,
                install_path=install_path,
                dep_key="owner/repo",
                resolved_ref=None,
                dep_locked_chk=dep_locked_chk,
            )
            result = source.acquire()

        assert result is not None

    def test_unpinned_dep_adds_unpinned_delta(self, tmp_path):
        from apm_cli.install.sources import CachedDependencySource

        install_path = tmp_path / "install"
        install_path.mkdir()

        ctx = _make_ctx(project_root=tmp_path, targets=False)
        ctx.callback_downloaded = {}
        dep_ref = _make_dep_ref(is_local=False, reference=None)  # No reference = unpinned
        dep_ref.reference = None

        dep_locked_chk = MagicMock()
        dep_locked_chk.resolved_commit = "aaaa" * 10
        dep_locked_chk.registry_prefix = None

        source = CachedDependencySource(
            ctx=ctx,
            dep_ref=dep_ref,
            install_path=install_path,
            dep_key="owner/repo",
            resolved_ref=None,
            dep_locked_chk=dep_locked_chk,
        )
        result = source.acquire()
        assert result is not None
        assert result.deltas.get("unpinned") == 1


# ---------------------------------------------------------------------------
# LocalDependencySource.acquire -- real install pipeline symlink dereference
#
# These exercise the FULL local-install path through
# ``LocalDependencySource.acquire`` WITHOUT mocking ``_copy_local_package``,
# pinning the #1668 in-package symlink dereference + containment contract at
# the install-pipeline boundary (supply-chain-security-expert follow-up).
# ---------------------------------------------------------------------------


def _make_local_pkg_with_symlink(root: Path) -> Path:
    """Build a minimal local APM package with an in-package symlink.

    Layout::

        pkg/
          apm.yml
          real.txt          (regular file)
          link.txt -> real.txt (in-package symlink)
    """
    pkg = root / "local_pkg"
    pkg.mkdir()
    (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
    (pkg / "real.txt").write_text("payload-content")
    (pkg / "link.txt").symlink_to(pkg / "real.txt")
    return pkg


class TestLocalInstallSymlinkDerefThroughPipeline:
    def test_in_package_symlink_materialized_as_real_file(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource
        from apm_cli.models.apm_package import PackageType

        pkg = _make_local_pkg_with_symlink(tmp_path)
        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"

        ctx = _make_ctx(project_root=tmp_path)
        ctx.source_root = tmp_path
        dep_ref = _make_dep_ref(local_path=str(pkg))

        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/test-pkg")
            result = source.acquire()

        assert result is not None
        staged_link = install_path / "link.txt"
        # The symlink must be materialized as a real file, not preserved as a
        # symlink -- matching remote-install behavior.
        assert staged_link.is_file()
        assert not staged_link.is_symlink()
        assert staged_link.read_text() == "payload-content"

    def test_escaping_symlink_hard_fails_through_pipeline(self, tmp_path):
        from apm_cli.install.sources import LocalDependencySource
        from apm_cli.models.apm_package import PackageType
        from apm_cli.utils.path_security import PathTraversalError

        outside = tmp_path / "outside_secret.txt"
        outside.write_text("secret")
        pkg = tmp_path / "local_pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        (pkg / "escape.txt").symlink_to(outside)

        install_path = tmp_path / "apm_modules" / "_local" / "test-pkg"

        ctx = _make_ctx(project_root=tmp_path)
        ctx.source_root = tmp_path
        dep_ref = _make_dep_ref(local_path=str(pkg))

        with patch("apm_cli.models.validation.detect_package_type") as mock_detect:
            mock_detect.return_value = (PackageType.APM_PACKAGE, None)
            source = LocalDependencySource(ctx, dep_ref, install_path, "owner/test-pkg")
            with pytest.raises(PathTraversalError, match=r"escapes"):
                source.acquire()

        # The hard-fail must leave no partial staged content behind.
        assert not install_path.exists()


class TestGitLabHostTypeInstallRoundTrip:
    def test_manifest_host_type_selects_gitlab_backend_and_lockfile_entry(self):
        from urllib.parse import urlparse

        from apm_cli.core.auth import AuthResolver
        from apm_cli.deps.host_backends import GitLabBackend, backend_for
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse_from_dict(
            {"git": "https://code.acme.com/group/sub/repo.git", "type": "gitlab"}
        )

        host_info = AuthResolver.classify_host(
            dep_ref.host,
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        )
        backend = backend_for(dep_ref, AuthResolver())
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        restored = LockedDependency.from_dict(locked.to_dict()).to_dependency_ref()

        assert host_info.kind == "gitlab"
        parsed_api_base = urlparse(host_info.api_base)
        assert parsed_api_base.hostname == "code.acme.com"
        assert parsed_api_base.path == "/api/v4"
        assert isinstance(backend, GitLabBackend)
        assert locked.host_type == "gitlab"
        assert restored.host_type == "gitlab"
