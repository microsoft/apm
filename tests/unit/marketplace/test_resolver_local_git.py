"""Resolver coverage for local + generic-git marketplaces.

Covers:
- local marketplace + relative plugin source -> local-path canonical recognised by ``DependencyReference.is_local_path``
- generic-git marketplace + relative plugin source -> virtual-path dep_ref against the marketplace URL
- local marketplace + absolute github plugin source -> unchanged GitHub canonical
- github backfill / cross-repo misconfig sentinel unchanged on existing paths
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import resolve_marketplace_plugin
from apm_cli.models.dependency.reference import DependencyReference


def _patch_marketplace(source: MarketplaceSource, plugins: list[MarketplacePlugin]):
    manifest = MarketplaceManifest(
        name=source.name,
        owner_name="",
        plugins=plugins,
    )
    return (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
    )


def _plugin(name: str, source) -> MarketplacePlugin:
    return MarketplacePlugin(name=name, source=source)


def test_local_marketplace_relative_source_yields_local_path_canonical(tmp_path: Path) -> None:
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("my-skill", "./skills/my-skill")

    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("my-skill", "local-mkt")

    assert result.dependency_reference is None
    assert result.canonical == f"{tmp_path}/skills/my-skill"
    assert DependencyReference.is_local_path(result.canonical)


def test_local_marketplace_bare_name_source_with_plugin_root(tmp_path: Path) -> None:
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("hello", "hello")
    manifest = MarketplaceManifest(
        name="local-mkt", owner_name="", plugins=[plugin], plugin_root="plugins"
    )

    with (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=src),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
    ):
        result = resolve_marketplace_plugin("hello", "local-mkt")

    assert result.canonical == f"{tmp_path}/plugins/hello"


def test_local_marketplace_root_source_returns_repo_root(tmp_path: Path) -> None:
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("root-plugin", ".")
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("root-plugin", "local-mkt")
    assert result.canonical == str(tmp_path)


def test_local_marketplace_traversal_in_source_rejected(tmp_path: Path) -> None:
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("evil", "../escape")
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2, pytest.raises(ValueError):
        resolve_marketplace_plugin("evil", "local-mkt")


def test_generic_git_marketplace_relative_source_builds_virtual_path_dep_ref() -> None:
    src = MarketplaceSource(
        name="gitea-mkt", url="https://gitea.example.com/org/repo.git", ref="main"
    )
    plugin = _plugin("my-skill", "./skills/my-skill")
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("my-skill", "gitea-mkt")

    assert result.dependency_reference is not None
    dep_ref = result.dependency_reference
    assert dep_ref.virtual_path == "skills/my-skill"
    assert dep_ref.host == "gitea.example.com"
    assert "org/repo" in dep_ref.repo_url


def test_local_marketplace_absolute_github_source_keeps_github_canonical(tmp_path: Path) -> None:
    """Plugin pointing to an absolute GitHub repo is fetched from GitHub regardless of where the marketplace lives."""
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("absolute", {"type": "github", "repo": "github.com/foo/bar"})
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("absolute", "local-mkt")

    # Absolute github source still resolves to an owner/repo canonical
    assert "foo/bar" in result.canonical


def test_ado_marketplace_relative_source_builds_virtual_path_dep_ref() -> None:
    """ADO is a new first-class host; in-marketplace plugins go through explicit git+path."""
    src = MarketplaceSource(
        name="ado-mkt",
        url="https://dev.azure.com/contoso/eng/_git/agent-forge",
        ref="main",
    )
    plugin = _plugin("skill-foo", "./skills/skill-foo")
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("skill-foo", "ado-mkt")

    assert result.dependency_reference is not None
    assert result.dependency_reference.virtual_path == "skills/skill-foo"
    assert result.dependency_reference.host == "dev.azure.com"
    assert result.dependency_reference.ado_organization == "contoso"
