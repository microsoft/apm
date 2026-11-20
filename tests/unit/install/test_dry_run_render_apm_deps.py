"""Dry-run rendering of APM dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_apm_dep(
    repo_url: str = "owner/repo",
    reference: str = "main",
) -> MagicMock:
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.reference = reference
    dep.get_unique_key.return_value = repo_url
    dep.to_display_reference.return_value = f"{repo_url}#{reference or 'main'}"
    return dep


@dataclass(frozen=True)
class _DisplayDependency:
    name: str

    def __str__(self) -> str:
        return self.name


def _make_mcp_dep(name: str = "my-server") -> _DisplayDependency:
    return _DisplayDependency(name)


# ---------------------------------------------------------------------------
# APM, MCP, and LSP dependency rendering
# ---------------------------------------------------------------------------


class TestRenderApmDeps:
    def test_plan_counts_only_selected_dependency_types(self) -> None:
        """Preview counts exclude dependency types filtered from the install."""
        plan = ProspectiveInstallPlan(
            apm_dependencies=(_make_apm_dep(),),
            dev_apm_dependencies=(),
            selected_apm_dependencies=(_make_apm_dep(),),
            mcp_dependencies=(_make_mcp_dep(),),
            should_install_apm=False,
            should_install_mcp=True,
            only_packages=None,
        )

        assert plan.apm_dependency_count == 0
        assert plan.mcp_dependency_count == 1
        assert plan.lsp_dependency_count == 0
        assert plan.dependency_counts == (0, 1, 0)

    def test_plan_selects_only_requested_dependency_without_reparsing_it(self) -> None:
        """Preview selection retains the interpreted dependency object."""
        existing = DependencyReference.parse("owner/existing#1.0.0")
        requested = DependencyReference.parse("owner/requested#2.0.0")
        requested.source = "registry"
        requested.registry_name = "private"
        package = MagicMock()
        package.get_apm_dependencies.return_value = [existing, requested]
        package.get_dev_apm_dependencies.return_value = []
        package.get_all_mcp_dependencies.return_value = []
        package.get_lsp_dependencies.return_value = []

        plan = ProspectiveInstallPlan.from_apm_package(
            package,
            should_install_apm=True,
            should_install_mcp=True,
            only_packages=["owner/requested#2.0.0"],
        )

        assert plan.selected_apm_dependencies == (requested,)
        assert plan.selected_apm_dependencies[0].source == "registry"
        assert plan.selected_apm_dependencies[0].registry_name == "private"

    def test_plan_retains_structured_versioned_path_for_selection_and_updates(self) -> None:
        requested = DependencyReference.parse_from_dict(
            {
                "git": "https://github.com/acme/catalog",
                "path": "plugins/tool-1.2.3",
                "ref": "release",
            }
        )
        package = MagicMock()
        package.get_apm_dependencies.return_value = [requested]
        package.get_dev_apm_dependencies.return_value = []
        package.get_all_mcp_dependencies.return_value = []
        package.get_lsp_dependencies.return_value = []

        plan = ProspectiveInstallPlan.from_apm_package(
            package,
            should_install_apm=True,
            should_install_mcp=False,
            only_packages=["acme/catalog/plugins/tool-1.2.3#release"],
            updated_packages=["acme/catalog/plugins/tool-1.2.3#release"],
        )

        assert plan.selected_apm_dependencies == (requested,)
        assert plan.updated_apm_identities == {"acme/catalog/plugins/tool-1.2.3"}

    def test_plan_excludes_apm_selection_when_only_mcp_is_requested(self) -> None:
        """The prospective plan does not leak APM work into --only=mcp previews."""
        dep = DependencyReference.parse("owner/repo#main")
        package = MagicMock()
        package.get_apm_dependencies.return_value = [dep]
        package.get_dev_apm_dependencies.return_value = []
        package.get_all_mcp_dependencies.return_value = []
        package.get_lsp_dependencies.return_value = []

        plan = ProspectiveInstallPlan.from_apm_package(
            package,
            should_install_apm=False,
            should_install_mcp=True,
            only_packages=None,
        )

        assert plan.selected_apm_dependencies == ()
        assert plan.apm_dependency_count == 0


# ---------------------------------------------------------------------------
# Orphan preview rendering
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dry-run notice and success message
# ---------------------------------------------------------------------------
