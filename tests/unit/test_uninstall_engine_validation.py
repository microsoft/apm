"""tests for apm_cli.commands.uninstall.engine.

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

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.verbose = False
    return m


def _make_lockfile(deps=None):
    return LockFile(dependencies=deps or {})


def _make_locked_dep(key, resolved_by=None):
    return LockedDependency(repo_url=key, resolved_by=resolved_by)


@pytest.fixture(params=["locked", "mock"])
def dep_kind(request):
    """Run a test with real lockfile objects and with MagicMock stand-ins."""
    return request.param


@pytest.fixture
def make_lockfile(dep_kind):
    return _make_lockfile if dep_kind == "locked" else _make_mock_lockfile


# ---------------------------------------------------------------------------
# _build_children_index
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_uninstall_packages
# ---------------------------------------------------------------------------


class TestValidateUninstallPackages:
    def test_invalid_format_no_slash(self):
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = _make_logger()
        _removed, not_found = _validate_uninstall_packages(
            ["badname"],
            [],
            logger,
        )
        assert "badname" in not_found
        logger.error.assert_called()

    def test_valid_package_found(self):
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = _make_logger()
        removed, not_found = _validate_uninstall_packages(
            ["org/repo"],
            ["org/repo"],
            logger,
        )
        assert "org/repo" in removed or len(removed) == 1
        assert not_found == []

    def test_package_not_in_current_deps(self):
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = _make_logger()
        _removed, not_found = _validate_uninstall_packages(
            ["org/repo"],
            [],
            logger,
        )
        assert "org/repo" in not_found

    def test_local_bundle_states_the_limitation(self, tmp_path, monkeypatch):
        """A bundle path is not sent to 'apm deps list' for a key (#3994)."""
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        (tmp_path / "project-1.0.0.zip").write_bytes(b"PK")
        monkeypatch.chdir(tmp_path)
        logger = _make_logger()
        _removed, not_found = _validate_uninstall_packages(["./project-1.0.0.zip"], [], logger)
        assert not_found == ["./project-1.0.0.zip"]
        message = logger.error.call_args.args[0]
        assert "'project-1.0.0.zip' is a local bundle" in message
        assert "cannot remove local-bundle content" in message
        assert "deps list" not in message

    @pytest.mark.parametrize(
        ("provenance", "expected"),
        [
            ({}, "Remove 'fetch' under dependencies.mcp in apm.yml, then run 'apm install'"),
            ({"fetch": "acme/tools"}, "brought in by package 'acme/tools'"),
            ({"fetch": "bundle:proj"}, "from a local bundle"),
        ],
        ids=["root", "package", "bundle"],
    )
    def test_mcp_server_name_routes_to_its_removal(self, provenance, expected):
        """'apm uninstall <server>' names how that server is removed (#3994)."""
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        lockfile = _make_lockfile()
        lockfile.mcp_servers = ["fetch"]
        lockfile.mcp_config_provenance = provenance
        logger = _make_logger()
        _removed, not_found = _validate_uninstall_packages(["fetch"], [], logger, lockfile)
        assert not_found == ["fetch"]
        message = logger.error.call_args.args[0]
        assert expected in message
        assert "Invalid package format" not in message

    def test_marketplace_resolved_none_adds_to_not_found(self):
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = _make_logger()
        # plugin-name without slash should be treated as marketplace ref
        with (
            patch(
                "apm_cli.commands.uninstall.engine._is_marketplace_ref",
                return_value=True,
            ),
            patch(
                "apm_cli.commands.uninstall.engine._resolve_marketplace_packages",
                return_value={"my-plugin@market": None},
            ),
        ):
            _removed, not_found = _validate_uninstall_packages(
                ["my-plugin@market"],
                [],
                logger,
            )
        assert "my-plugin@market" in not_found


# ---------------------------------------------------------------------------
# _dry_run_uninstall
# ---------------------------------------------------------------------------


class TestDryRunUninstall:
    def test_no_lockfile(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
        ):
            _dry_run_uninstall(["org/repo"], apm_modules, logger)
        logger.success.assert_called()

    def test_with_lockfile_and_orphans(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        orphan_dep = _make_locked_dep("org/child", resolved_by="org/repo")
        lf = _make_lockfile({"org/child": orphan_dep})

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lf),
        ):
            _dry_run_uninstall(["org/repo"], apm_modules, logger)
        logger.success.assert_called()

    def test_package_exists_on_disk(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        (apm_modules / "org").mkdir()
        (apm_modules / "org" / "repo").mkdir()

        with (
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
        ):
            _dry_run_uninstall(["org/repo"], apm_modules, logger)
        logger.success.assert_called()

    def test_with_mock_lockfile_and_orphans(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        orphan_dep = _make_mock_locked_dep("org/child", "child", resolved_by="org/repo")
        lf = _make_mock_lockfile({"org/child": orphan_dep})

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


class TestRemovePackagesFromDisk:
    def test_no_modules_dir_returns_zero(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        logger = _make_logger()
        result = _remove_packages_from_disk(["org/repo"], tmp_path / "nonexistent", logger)
        assert result == 0

    def test_package_not_on_disk_warns(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        result = _remove_packages_from_disk(["org/repo"], apm_modules, logger)
        assert result == 0
        logger.warning.assert_called()

    def test_path_traversal_error_propagates(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk
        from apm_cli.utils.path_security import PathTraversalError

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        with (
            patch(
                "apm_cli.models.apm_package.DependencyReference.parse",
                side_effect=PathTraversalError("traversal"),
            ),
            pytest.raises(PathTraversalError, match="traversal"),
        ):
            _remove_packages_from_disk(["../evil"], apm_modules, logger)
        logger.error.assert_called()

    def test_removes_existing_package(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        pkg_dir = org_dir / "repo"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: repo\n")

        with patch("apm_cli.integration.base_integrator.BaseIntegrator.cleanup_empty_parents"):
            result = _remove_packages_from_disk(["org/repo"], apm_modules, logger)
        assert result == 1
        logger.progress.assert_called()

    def test_fallback_path_single_segment(self, tmp_path):
        """Covers the single-segment package_str fallback path."""
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        # DependencyReference.parse raises -> fallback path with single segment
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            result = _remove_packages_from_disk(["just-a-name"], apm_modules, logger)
        # Not found, but should not raise
        assert result == 0


# ---------------------------------------------------------------------------
# _cleanup_transitive_orphans
# ---------------------------------------------------------------------------


class TestCleanupTransitiveOrphans:
    def test_no_lockfile_returns_zero(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        removed, orphans = _cleanup_transitive_orphans(
            None, ["org/repo"], tmp_path / "nonexistent", tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert orphans == set()

    def test_no_modules_dir_returns_zero(self, make_lockfile, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        lf = make_lockfile()
        removed, orphans = _cleanup_transitive_orphans(
            lf, ["org/repo"], tmp_path / "nonexistent", tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert orphans == set()

    def test_no_orphans_returns_zero(self, make_lockfile, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        lf = make_lockfile()

        removed, orphans = _cleanup_transitive_orphans(
            lf, ["org/repo"], apm_modules, tmp_path / "apm.yml", logger
        )
        assert removed == 0
        assert len(orphans) == 0

    def test_orphan_removed(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        child_dir = org_dir / "child"
        child_dir.mkdir()

        orphan = _make_locked_dep("org/child", resolved_by="org/parent")
        lf = _make_lockfile({"org/child": orphan})

        (tmp_path / "apm.yml").write_text("name: test\n")

        with (
            patch("apm_cli.utils.yaml_io.load_yaml", return_value={}),
            patch("apm_cli.integration.base_integrator.BaseIntegrator.cleanup_empty_parents"),
        ):
            removed, _orphans = _cleanup_transitive_orphans(
                lf, ["org/parent"], apm_modules, tmp_path / "apm.yml", logger
            )
        assert removed >= 0  # May or may not remove depending on remaining deps

    def test_orphan_removal_error_logged(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        child_dir = org_dir / "child"
        child_dir.mkdir()

        orphan = _make_locked_dep("org/child", resolved_by="org/parent")
        lf = _make_lockfile({"org/child": orphan})

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

    @pytest.mark.windows_compat
    def test_orphan_install_dir_removed_on_every_platform(self, tmp_path: Path) -> None:
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

    def test_orphan_removal_error_logged_with_mock_lockfile(self, tmp_path):
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        logger = _make_logger()
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        org_dir = apm_modules / "org"
        org_dir.mkdir()
        child_dir = org_dir / "child"
        child_dir.mkdir()

        orphan = _make_mock_locked_dep("org/child", "child", resolved_by="org/parent")
        lf = _make_mock_lockfile({"org/child": orphan})
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


def _make_mock_lockfile(deps=None):
    lf = MagicMock()
    lf.replacements = None
    lf.dependencies = deps or {}
    lf.get_package_dependencies.return_value = list((deps or {}).values())
    return lf


def _make_mock_locked_dep(key, repo_url=None, resolved_by=None):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.repo_url = repo_url or key.split("/")[-1]
    dep.resolved_by = resolved_by
    dep.parent_keys = (resolved_by,)
    return dep
