"""Integration guardrails for preserving declared dependency intent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def test_incompatible_refs_survive_to_conflict_selection(tmp_path: Path) -> None:
    """Two constraints for one package must be reported, not queue-deduped."""
    from apm_cli.deps.apm_resolver import APMDependencyResolver

    (tmp_path / "apm.yml").write_text(
        "\n".join(
            (
                "name: root",
                "version: 1.0.0",
                "dependencies:",
                "  apm:",
                "    - owner/shared#v1",
                "    - owner/shared#v2",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    graph = APMDependencyResolver(max_parallel=1).resolve_dependencies(tmp_path)

    assert graph.has_conflicts()
    conflict = graph.flattened_dependencies.conflicts[0]
    assert conflict.winner.reference == "v1"
    assert [item.reference for item in conflict.conflicts] == ["v2"]


def test_transitive_local_identity_includes_parent_and_anchor(tmp_path: Path) -> None:
    """Equal relative paths from different parents must have distinct identity."""
    from apm_cli.models.dependency.reference import DependencyReference

    first = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../shared",
        source="local",
        declaring_parent="owner/parent-a#main",
        anchored_local_path=str(tmp_path / "a" / "shared"),
    )
    second = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../shared",
        source="local",
        declaring_parent="owner/parent-b#main",
        anchored_local_path=str(tmp_path / "b" / "shared"),
    )

    assert first.get_unique_key() != second.get_unique_key()
    assert first.get_install_path(tmp_path / "apm_modules") != second.get_install_path(
        tmp_path / "apm_modules"
    )


def test_configured_mcp_registry_url_is_used(monkeypatch) -> None:
    """The URL shown by the command must be the URL passed to its client."""
    from apm_cli.commands import mcp

    captured: list[str | None] = []

    class FakeRegistry:
        def __init__(self, registry_url=None):
            captured.append(registry_url)
            self.client = MagicMock(registry_url=registry_url)

    monkeypatch.delenv(mcp.MCP_REGISTRY_ENV, raising=False)
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda: "https://registry.test/v0")
    monkeypatch.setattr("apm_cli.registry.integration.RegistryIntegration", FakeRegistry)

    registry = mcp._build_registry_with_diag(None, MagicMock())

    assert captured == ["https://registry.test/v0"]
    assert registry.client.registry_url == captured[0]


def test_marketplace_registry_routing_returns_registry_dependency(monkeypatch) -> None:
    """Registry intent must reach the package-registry resolver contract."""
    from apm_cli.marketplace.models import (
        MarketplaceManifest,
        MarketplacePlugin,
        MarketplaceSource,
    )
    from apm_cli.marketplace.resolver import resolve_marketplace_plugin

    source = MarketplaceSource(name="catalog", url="https://example.test/catalog.git")
    manifest = MarketplaceManifest(
        name="catalog",
        plugins=(
            MarketplacePlugin(
                name="owner/tool",
                source="ignored/git/path",
                version="^1.2.0",
                registry="internal",
            ),
        ),
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.resolver.get_marketplace_by_name", lambda _name: source
    )
    monkeypatch.setattr("apm_cli.marketplace.resolver.fetch_or_cache", lambda *_a, **_k: manifest)

    resolution = resolve_marketplace_plugin("owner/tool", "catalog")

    dep = resolution.dependency_reference
    assert dep is not None
    assert dep.source == "registry"
    assert dep.registry_name == "internal"
    assert dep.reference == "^1.2.0"
