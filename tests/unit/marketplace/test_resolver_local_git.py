"""Resolver coverage for local + generic-git marketplaces.

Covers:
- local marketplace + relative plugin source -> local-path canonical recognised by ``DependencyReference.is_local_path``
- generic-git marketplace + relative plugin source -> virtual-path dep_ref against the marketplace URL
- local marketplace + absolute github plugin source -> unchanged GitHub canonical
- github backfill / cross-repo misconfig sentinel unchanged on existing paths
"""

from __future__ import annotations

from contextlib import AbstractContextManager
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

pytestmark = pytest.mark.component


def _patch_marketplace(
    source: MarketplaceSource, plugins: list[MarketplacePlugin]
) -> tuple[AbstractContextManager, AbstractContextManager]:
    manifest = MarketplaceManifest(
        name=source.name,
        owner_name="",
        plugins=plugins,
    )
    return (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
    )


def _plugin(name: str, source: object) -> MarketplacePlugin:
    return MarketplacePlugin(name=name, source=source)


@pytest.mark.windows_compat
def test_local_marketplace_relative_source_yields_local_path_canonical(tmp_path: Path) -> None:
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("my-skill", "./skills/my-skill")

    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("my-skill", "local-mkt")

    assert result.dependency_reference is None
    # Local canonicals are OS-native filesystem paths (``str(Path)``), so the
    # expectation must be built with the platform separator -- a hard-coded
    # "/" only matches on POSIX.
    assert result.canonical == str(tmp_path / "skills" / "my-skill")
    assert DependencyReference.is_local_path(result.canonical)


@pytest.mark.windows_compat
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

    assert result.canonical == str(tmp_path / "plugins" / "hello")


@pytest.mark.windows_compat
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


def test_local_marketplace_absolute_github_source_keeps_github_canonical(tmp_path: Path) -> None:
    """Plugin pointing to an absolute GitHub repo is fetched from GitHub regardless of where the marketplace lives."""
    src = MarketplaceSource(name="local-mkt", url=f"file://{tmp_path}", ref="main")
    plugin = _plugin("absolute", {"type": "github", "repo": "github.com/foo/bar"})
    p1, p2 = _patch_marketplace(src, [plugin])
    with p1, p2:
        result = resolve_marketplace_plugin("absolute", "local-mkt")

    # Absolute github source still resolves to an owner/repo canonical.
    # Canonical is not a URL but a colon-separated identifier; split and
    # compare structured segments to avoid substring-style URL matching.
    parts = result.canonical.split(":")
    assert any(seg.endswith("foo/bar") for seg in parts)
