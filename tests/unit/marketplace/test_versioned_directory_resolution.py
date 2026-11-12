"""Marketplace paths retain their explicit repository boundary despite dots."""

from unittest.mock import patch

import pytest

from apm_cli.install.package_resolution import dependency_reference_to_yaml_entry
from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin, MarketplaceSource
from apm_cli.marketplace.resolver import resolve_marketplace_plugin
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import VirtualPackageType


@pytest.mark.parametrize("host", ["github.com", "corp.ghe.com"])
@pytest.mark.parametrize(
    ("path", "expected_type"),
    [
        ("plugins/my-plugin-1.2.3", VirtualPackageType.SUBDIRECTORY),
        ("collections/my-plugin-2026.9.3", VirtualPackageType.SUBDIRECTORY),
        ("plugins/my-plugin-1.2.3-rc.1", VirtualPackageType.SUBDIRECTORY),
        ("plugins/my-plugin", VirtualPackageType.SUBDIRECTORY),
        ("prompts/review.prompt.md", VirtualPackageType.FILE),
        ("instructions/review.instructions.md", VirtualPackageType.FILE),
        ("agents/review.agent.md", VirtualPackageType.FILE),
    ],
)
def test_marketplace_path_classification_and_manifest_round_trip(
    host: str, path: str, expected_type: VirtualPackageType
) -> None:
    source = MarketplaceSource(name="catalog", url=f"https://{host}/acme/catalog", ref="release")
    plugin = MarketplacePlugin(name="plugin", source=f"./{path}")
    manifest = MarketplaceManifest(name="catalog", plugins=(plugin,))
    with (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
    ):
        resolution = resolve_marketplace_plugin("plugin", "catalog")

    dep = resolution.dependency_reference or DependencyReference.parse(resolution.canonical)
    assert (dep.host, dep.repo_url, dep.virtual_path, dep.reference) == (
        host,
        "acme/catalog",
        path,
        "release",
    )
    assert dep.virtual_type == expected_type
    entry = dependency_reference_to_yaml_entry(dep)
    replayed = DependencyReference.parse_from_dict(entry)
    assert replayed.virtual_path == path
    assert replayed.virtual_type == expected_type


@pytest.mark.parametrize("path", ["../my-plugin-1.2.3", "plugins/../my-plugin-1.2.3"])
def test_versioned_marketplace_paths_reject_traversal(path: str) -> None:
    source = MarketplaceSource(name="catalog", url="https://github.com/acme/catalog")
    plugin = MarketplacePlugin(name="plugin", source=path)
    manifest = MarketplaceManifest(name="catalog", plugins=(plugin,))
    with (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
        pytest.raises(ValueError, match="traversal"),
    ):
        resolve_marketplace_plugin("plugin", "catalog")
