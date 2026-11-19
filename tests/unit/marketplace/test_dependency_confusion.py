"""Unit traps for the #1326 marketplace admission owner (#3913)."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import DependencyTree
from apm_cli.marketplace.dependency_confusion import (
    DependencyConfusionError,
    admit_marketplace_resolution,
)
from apm_cli.marketplace.resolver import CrossRepoMisconfigRisk, MarketplacePluginResolution
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference

_RISK = CrossRepoMisconfigRisk(
    marketplace_host="corp.ghe.com",
    bare_repo_field="acme/tools",
    suggested_qualified_repo="corp.ghe.com/acme/tools",
)


def _resolution(risk: CrossRepoMisconfigRisk | None) -> MarketplacePluginResolution:
    return MarketplacePluginResolution(
        canonical="acme/tools", plugin=Mock(manifest=None), cross_repo_misconfig_risk=risk
    )


def test_admission_passes_resolution_without_sentinel() -> None:
    admit_marketplace_resolution(_resolution(None))


def test_admission_refuses_sentinel_naming_both_escape_hatches() -> None:
    with pytest.raises(DependencyConfusionError) as refused:
        admit_marketplace_resolution(_resolution(_RISK))
    message = str(refused.value)
    assert "#1326" in message
    assert "'corp.ghe.com/acme/tools'" in message
    assert "'github.com/acme/tools'" in message


@patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
def test_resolver_records_refusal_as_resolution_error(mock_resolve: Mock) -> None:
    """The resolver path reports the refusal as a named resolution error, never a crash."""
    mock_resolve.return_value = _resolution(_RISK)
    dep = DependencyReference(
        repo_url="_marketplace/corp-mkt/shared-tool",
        is_marketplace=True,
        marketplace_name="corp-mkt",
        marketplace_plugin_name="shared-tool",
    )
    tree = DependencyTree(root_package=APMPackage(name="root", version="1.0.0"))

    assert APMDependencyResolver()._resolve_marketplace_or_record_error(dep, tree, "") is None
    assert len(tree.resolution_errors) == 1
    assert "dependency-confusion risk #1326" in tree.resolution_errors[0]
