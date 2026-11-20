"""Resolver coverage for local + generic-git marketplaces (pure logic cases)."""

from __future__ import annotations

from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import resolve_marketplace_plugin

pytestmark = pytest.mark.unit


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
    # repo_url here is the owner/repo identifier, not a full URL; assert on
    # exact equality rather than substring (CodeQL py/incomplete-url-substring-sanitization).
    assert dep_ref.repo_url == "org/repo"


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
