"""Tests for ``apm.lock.yaml`` upstream provenance writing.

Covers the LockedUpstream / LockedUpstreamPlugin dataclasses and the
end-to-end path where ``MarketplaceBuilder.build()`` persists upstream
provenance into ``apm.lock.yaml`` after a successful build.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from apm_cli.deps.lockfile import (
    LockedUpstream,
    LockedUpstreamPlugin,
    LockFile,
    get_lockfile_path,
)
from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.upstream_cache import UpstreamCache
from apm_cli.marketplace.upstream_resolver import UpstreamResolver

SHA_DIRECT = "a" * 40
SHA_UPSTREAM_MANIFEST = "b" * 40
SHA_UPSTREAM_PLUGIN = "c" * 40


# ---------------------------------------------------------------------------
# Dataclass round-trips
# ---------------------------------------------------------------------------


class TestLockedUpstreamRoundTrip:
    def test_to_dict_includes_required_fields(self) -> None:
        up = LockedUpstream(
            alias="gitnexus",
            host="github.com",
            owner="abhigyanpatwari",
            repo="GitNexus",
            path=".claude-plugin/marketplace.json",
            manifest_sha=SHA_UPSTREAM_MANIFEST,
            canonical_full_name="abhigyanpatwari/GitNexus",
            refreshed_at="2024-01-01T00:00:00+00:00",
        )
        data = up.to_dict()
        assert data["host"] == "github.com"
        assert data["owner"] == "abhigyanpatwari"
        assert data["repo"] == "GitNexus"
        assert data["path"] == ".claude-plugin/marketplace.json"
        assert data["manifest_sha"] == SHA_UPSTREAM_MANIFEST
        assert data["canonical_full_name"] == "abhigyanpatwari/GitNexus"
        assert data["refreshed_at"] == "2024-01-01T00:00:00+00:00"
        # Plugins absent from output when empty.
        assert "plugins" not in data

    def test_to_dict_with_plugins_sorted_by_upstream_name(self) -> None:
        up = LockedUpstream(
            alias="gitnexus",
            host="github.com",
            owner="abhigyanpatwari",
            repo="GitNexus",
            path=".claude-plugin/marketplace.json",
            manifest_sha=SHA_UPSTREAM_MANIFEST,
            plugins={
                "zebra": LockedUpstreamPlugin(
                    upstream_name="zebra",
                    emitted_as="acme-zebra",
                    resolved_sha=SHA_UPSTREAM_PLUGIN,
                    resolved_source={"sha": SHA_UPSTREAM_PLUGIN},
                ),
                "alpha": LockedUpstreamPlugin(
                    upstream_name="alpha",
                    emitted_as="acme-alpha",
                    resolved_sha=SHA_DIRECT,
                    resolved_source={"sha": SHA_DIRECT},
                ),
            },
        )
        data = up.to_dict()
        # Keys sorted alphabetically for deterministic output.
        assert list(data["plugins"].keys()) == ["alpha", "zebra"]

    def test_round_trip_preserves_data(self) -> None:
        original = LockedUpstream(
            alias="gitnexus",
            host="github.com",
            owner="abhigyanpatwari",
            repo="GitNexus",
            path=".claude-plugin/marketplace.json",
            manifest_sha=SHA_UPSTREAM_MANIFEST,
            canonical_full_name="abhigyanpatwari/GitNexus",
            plugins={
                "gitnexus": LockedUpstreamPlugin(
                    upstream_name="gitnexus",
                    emitted_as="acme-gitnexus",
                    resolved_sha=SHA_UPSTREAM_PLUGIN,
                    resolved_source={
                        "host": "github.com",
                        "repo": "abhigyanpatwari/GitNexus",
                        "sha": SHA_UPSTREAM_PLUGIN,
                    },
                ),
            },
        )
        data = original.to_dict()
        restored = LockedUpstream.from_dict("gitnexus", data)
        assert restored.host == original.host
        assert restored.owner == original.owner
        assert restored.repo == original.repo
        assert restored.manifest_sha == original.manifest_sha
        assert restored.canonical_full_name == original.canonical_full_name
        assert "gitnexus" in restored.plugins
        plugin = restored.plugins["gitnexus"]
        assert plugin.emitted_as == "acme-gitnexus"
        assert plugin.resolved_sha == SHA_UPSTREAM_PLUGIN
        assert plugin.resolved_source["sha"] == SHA_UPSTREAM_PLUGIN


# ---------------------------------------------------------------------------
# LockFile YAML round-trip with upstreams
# ---------------------------------------------------------------------------


class TestLockFileUpstreams:
    def test_empty_upstreams_omitted_from_yaml(self) -> None:
        lock = LockFile()
        out = lock.to_yaml()
        assert "upstreams:" not in out

    def test_yaml_round_trip_preserves_upstreams(self) -> None:
        lock = LockFile()
        lock.upstreams["gitnexus"] = LockedUpstream(
            alias="gitnexus",
            host="github.com",
            owner="abhigyanpatwari",
            repo="GitNexus",
            path=".claude-plugin/marketplace.json",
            manifest_sha=SHA_UPSTREAM_MANIFEST,
            plugins={
                "gitnexus": LockedUpstreamPlugin(
                    upstream_name="gitnexus",
                    emitted_as="acme-gitnexus",
                    resolved_sha=SHA_UPSTREAM_PLUGIN,
                    resolved_source={"sha": SHA_UPSTREAM_PLUGIN},
                ),
            },
        )
        yaml_str = lock.to_yaml()
        assert "upstreams:" in yaml_str
        assert "gitnexus:" in yaml_str

        restored = LockFile.from_yaml(yaml_str)
        assert "gitnexus" in restored.upstreams
        gx = restored.upstreams["gitnexus"]
        assert gx.host == "github.com"
        assert gx.manifest_sha == SHA_UPSTREAM_MANIFEST
        assert "gitnexus" in gx.plugins


# ---------------------------------------------------------------------------
# Builder integration: writes upstream provenance after build
# ---------------------------------------------------------------------------


_YML = """\
name: acme-marketplace
description: ACME curated marketplace
version: 0.1.0
marketplace:
  owner:
    name: ACME Corp
  upstreams:
    - alias: gitnexus
      repo: abhigyanpatwari/GitNexus
      ref: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  packages:
    - name: acme-gitnexus
      upstream: gitnexus
      plugin: gitnexus
"""


def _gitnexus_manifest() -> dict:
    return {
        "name": "gitnexus-marketplace",
        "owner": {"name": "abhigyanpatwari"},
        "plugins": [
            {
                "name": "gitnexus",
                "description": "GitNexus plugin",
                "version": "1.0.0",
                "source": {
                    "type": "git-subdir",
                    "repo": "abhigyanpatwari/GitNexus",
                    "path": "gitnexus-claude-plugin",
                    "sha": SHA_UPSTREAM_PLUGIN,
                },
            }
        ],
    }


class _MockRefResolver:
    def __init__(self) -> None:
        self._refs: dict[str, list[RemoteRef]] = {}

    def list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
        from apm_cli.marketplace.errors import GitLsRemoteError

        raise GitLsRemoteError(package="", summary="Not used", hint="")

    def close(self) -> None:
        pass


def _patch_resolver_factory(builder: MarketplaceBuilder, *, cache: UpstreamCache) -> None:
    def _factory(yml):  # type: ignore[no-untyped-def]
        upstreams_by_alias = {u.alias: u for u in yml.upstreams}

        def _ref_to_sha(host: str, owner: str, repo: str, ref: str) -> str:
            return SHA_UPSTREAM_MANIFEST

        return UpstreamResolver(
            upstreams=upstreams_by_alias,
            cache=cache,
            ref_to_sha=_ref_to_sha,
            canonical_full_name=None,
        )

    builder._build_upstream_resolver = _factory  # type: ignore[assignment]


def test_build_writes_upstream_section_to_lockfile(tmp_path: Path) -> None:
    """A successful build with upstream packages writes provenance to apm.lock.yaml."""
    yml_path = tmp_path / "apm.yml"
    yml_path.write_text(_YML, encoding="utf-8")
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver()  # type: ignore[assignment]
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    assert report.errors == ()

    lockfile_path = get_lockfile_path(tmp_path)
    assert lockfile_path.exists()

    data = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
    assert "upstreams" in data
    assert "gitnexus" in data["upstreams"]
    upstream = data["upstreams"]["gitnexus"]
    assert upstream["host"] == "github.com"
    assert upstream["owner"] == "abhigyanpatwari"
    assert upstream["repo"] == "GitNexus"
    assert upstream["manifest_sha"] == SHA_UPSTREAM_MANIFEST
    assert "plugins" in upstream
    plugin = upstream["plugins"]["gitnexus"]
    assert plugin["emitted_as"] == "acme-gitnexus"
    assert plugin["resolved_sha"] == SHA_UPSTREAM_PLUGIN
    assert plugin["resolved_source"]["sha"] == SHA_UPSTREAM_PLUGIN


def test_build_does_not_inject_apm_metadata_when_lockfile_has_upstreams(
    tmp_path: Path,
) -> None:
    """marketplace.json must remain Anthropic-conformant even when lock has upstreams."""
    yml_path = tmp_path / "apm.yml"
    yml_path.write_text(_YML, encoding="utf-8")
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver()  # type: ignore[assignment]
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    doc = json.loads(report.output_path.read_text(encoding="utf-8"))
    # No top-level apm key, no per-plugin apm key, no per-plugin metadata.apm.
    assert "apm" not in doc
    for plugin in doc["plugins"]:
        assert "apm" not in plugin
        meta = plugin.get("metadata", {})
        if isinstance(meta, dict):
            assert "apm" not in meta
