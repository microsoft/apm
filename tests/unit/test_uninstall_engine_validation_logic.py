"""tests for apm_cli.commands.uninstall.engine (pure logic cases)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.unit

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


@pytest.fixture
def make_locked_dep(dep_kind):
    return _make_locked_dep if dep_kind == "locked" else _make_mock_locked_dep


def test_reachability_verbose_diagnostics_redact_local_paths():
    """Local reachability failures never reveal declared machine paths."""
    from apm_cli.commands.uninstall.engine import _warn_reachability_incomplete

    local_path = r"C:\Users\runner\Private Package"
    reachability = SimpleNamespace(unverifiable=[(local_path, f"manifest missing at {local_path}")])
    logger = _make_logger()
    logger.verbose = True

    _warn_reachability_incomplete(reachability, logger)

    rendered = "\n".join(call.args[0] for call in logger.verbose_detail.call_args_list)
    assert local_path not in rendered
    assert "local dependency" in rendered


def test_reintegration_verbose_detail_redacts_only_local_dependency_paths():
    """Remote errors retain detail while local declarations remain private."""
    from apm_cli.commands.uninstall.engine import _reintegration_error_detail
    from apm_cli.models.dependency import DependencyReference

    remote = DependencyReference.parse("owner/repo")
    local_path = r"C:\Users\runner\Private Package"
    local = DependencyReference.parse(local_path)
    error = RuntimeError("failed at remote primitive")

    assert _reintegration_error_detail(remote, error) == (
        "RuntimeError: failed at remote primitive"
    )
    local_detail = _reintegration_error_detail(local, RuntimeError(local_path))
    assert local_detail == "RuntimeError"
    assert local_path not in local_detail


# ---------------------------------------------------------------------------
# _build_children_index
# ---------------------------------------------------------------------------


class TestBuildChildrenIndex:
    def test_empty_deps(self, make_lockfile):
        from apm_cli.install.orphans import _build_children_index

        lf = make_lockfile()
        result = _build_children_index(lf)
        assert result == {}

    def test_no_resolved_by_skipped(self, make_lockfile, make_locked_dep):
        from apm_cli.install.orphans import _build_children_index

        dep = make_locked_dep("org/child", resolved_by=None)
        lf = make_lockfile({"org/child": dep})
        result = _build_children_index(lf)
        assert result == {}

    def test_resolved_by_populated(self, make_lockfile, make_locked_dep):
        from apm_cli.install.orphans import _build_children_index

        parent_url = "org/parent"
        child = make_locked_dep("org/child", resolved_by=parent_url)
        lf = make_lockfile({"org/child": child})
        result = _build_children_index(lf)
        assert parent_url in result
        assert child in result[parent_url]


# ---------------------------------------------------------------------------
# _validate_uninstall_packages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _dry_run_uninstall
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _remove_packages_from_disk
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _cleanup_transitive_orphans
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_marketplace_packages -- dry_run and registry paths
# ---------------------------------------------------------------------------


class TestResolveMarketplacePackages:
    def test_dry_run_skips_registry(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()
        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("plugin", "market", None),
        ):
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,
                logger,
                dry_run=True,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_registry_fallback_resolves_canonical(self, make_lockfile, make_locked_dep):
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
            lf = make_lockfile({"org/repo": make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] == "org/repo"

    def test_registry_fallback_not_in_lockfile_refused(self, make_lockfile, make_locked_dep):
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
            lf = make_lockfile({"org/repo": make_locked_dep("org/repo")})
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                lf,
                logger,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_registry_fallback_exception_logged(self):
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = _make_logger()

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("network error"),
            ),
        ):
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,
                logger,
            )
        assert result["plugin@market"] is None
        logger.warning.assert_called()

    def test_no_lockfile_trusts_registry(self):
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
            result = _resolve_marketplace_packages(
                ["plugin@market"],
                None,  # No lockfile
                logger,
            )
        assert result["plugin@market"] == "org/repo"


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
