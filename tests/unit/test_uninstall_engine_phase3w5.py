"""Phase-3w5 tests for apm_cli.commands.uninstall.engine.

Covers missing lines/branches identified in coverage-unit.json:
- _build_children_index: empty lockfile deps
- _resolve_marketplace_packages: dry_run skip, registry fallback paths (lines 126-181)
- _validate_uninstall_packages: invalid format, marketplace resolved None,
  canonical match found/not found (lines 231-288)
- _dry_run_uninstall: with/without lockfile, transitive orphans (lines 291-333)
- _remove_packages_from_disk: PathTraversalError, fallback path, exists/not exists (lines 336-375)
- _cleanup_transitive_orphans: no lockfile, empty orphans, actual removal (lines 378-461)
- _cleanup_stale_mcp: basic path coverage (lines 670+)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.verbose = False
    return m


def _make_lockfile(deps=None):
    lf = MagicMock()
    lf.replacements = None
    lf.dependencies = deps or {}
    lf.get_package_dependencies.return_value = list((deps or {}).values())
    return lf


def _make_locked_dep(key, repo_url=None, resolved_by=None):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.repo_url = repo_url or key.split("/")[-1]
    dep.resolved_by = resolved_by
    dep.parent_keys = (resolved_by,)
    return dep


# ---------------------------------------------------------------------------
# _build_children_index
# ---------------------------------------------------------------------------


class TestBuildChildrenIndex:
    def test_empty_deps(self):
        from apm_cli.install.orphans import _build_children_index

        lf = _make_lockfile()
        result = _build_children_index(lf)
        assert result == {}

    def test_no_resolved_by_skipped(self):
        from apm_cli.install.orphans import _build_children_index

        dep = _make_locked_dep("org/child", resolved_by=None)
        lf = _make_lockfile({"org/child": dep})
        result = _build_children_index(lf)
        assert result == {}

    def test_resolved_by_populated(self):
        from apm_cli.install.orphans import _build_children_index

        parent_url = "org/parent"
        child = _make_locked_dep("org/child", resolved_by=parent_url)
        lf = _make_lockfile({"org/child": child})
        result = _build_children_index(lf)
        assert parent_url in result
        assert child in result[parent_url]


# ---------------------------------------------------------------------------
# _validate_uninstall_packages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _dry_run_uninstall
# ---------------------------------------------------------------------------


class TestDryRunUninstall:
    def test_with_lockfile_and_orphans(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        orphan_dep = _make_locked_dep("org/child", "child", resolved_by="org/repo")
        lf = _make_lockfile({"org/child": orphan_dep})

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lf),
        ):
            _dry_run_uninstall(["org/repo"], apm_modules, logger)
        logger.success.assert_called()


# ---------------------------------------------------------------------------
# _remove_packages_from_disk
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _cleanup_transitive_orphans
# ---------------------------------------------------------------------------


class TestCleanupTransitiveOrphans:
    def test_no_modules_dir_returns_zero(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        lf = _make_lockfile()
        removed, orphans = _cleanup_transitive_orphans(
            lf, ["org/repo"], tmp_path / "nonexistent", tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert orphans == set()

    def test_no_orphans_returns_zero(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        lf = _make_lockfile()

        removed, orphans = _cleanup_transitive_orphans(
            lf, ["org/repo"], apm_modules, tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert len(orphans) == 0

    @pytest.mark.windows_compat
    def test_orphan_removed(self, tmp_path: Path) -> None:
        """An orphan's real install directory is removed on every platform."""
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        child_dir = org_dir / "child"
        child_dir.mkdir()

        orphan = LockedDependency(repo_url="org/child", resolved_by="org/parent")
        lf = LockFile(dependencies={orphan.get_unique_key(): orphan})

        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.utils.yaml_io.load_yaml", return_value={}),
            patch("apm_cli.integration.base_integrator.BaseIntegrator.cleanup_empty_parents"),
        ):
            removed, orphans = _cleanup_transitive_orphans(
                lf, ["org/parent"], apm_modules, tmp_path / "apm.yml", logger
            )
        assert removed == 1
        assert orphans == {"org/child"}
        assert not child_dir.exists()
        logger.error.assert_not_called()

    def test_orphan_removal_error_logged(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        child_dir = org_dir / "child"
        child_dir.mkdir()

        orphan = _make_locked_dep("org/child", "child", resolved_by="org/parent")
        lf = _make_lockfile({"org/child": orphan})
        lf.get_dependency.return_value = orphan

        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.utils.yaml_io.load_yaml", return_value={}),
            patch(
                "apm_cli.commands.uninstall.engine.safe_rmtree",
                side_effect=OSError("permission denied"),
            ),
            patch("apm_cli.integration.base_integrator.BaseIntegrator.cleanup_empty_parents"),
            pytest.raises(OSError, match="permission denied"),
        ):
            _cleanup_transitive_orphans(
                lf, ["org/parent"], apm_modules, tmp_path / "apm.yml", logger
            )
        # Error logged
        logger.error.assert_called()


# ---------------------------------------------------------------------------
# _resolve_marketplace_packages -- dry_run and registry paths
# ---------------------------------------------------------------------------


class TestResolveMarketplacePackages:
    def test_registry_fallback_resolves_canonical(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        mock_resolution = MagicMock()
        mock_resolution.canonical = "org/repo"

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=mock_resolution,
            ),
        ):
            lf = _make_lockfile({"org/repo": _make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] == "org/repo"

    def test_registry_fallback_not_in_lockfile_refused(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        mock_resolution = MagicMock()
        mock_resolution.canonical = "org/other-repo"

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=mock_resolution,
            ),
        ):
            lf = _make_lockfile({"org/repo": _make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()
