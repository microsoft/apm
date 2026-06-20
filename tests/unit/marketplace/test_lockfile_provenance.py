"""Tests for lockfile provenance fields -- serialization round-trip and backward compat."""

from types import SimpleNamespace
from urllib.parse import urlparse

import pytest  # noqa: F401

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


class TestLockedDependencyProvenance:
    """Verify marketplace provenance fields round-trip correctly."""

    def test_default_none(self):
        dep = LockedDependency(repo_url="owner/repo")
        assert dep.discovered_via is None
        assert dep.marketplace_plugin_name is None
        assert dep.source_url is None
        assert dep.source_digest is None

    def test_to_dict_omits_none(self):
        dep = LockedDependency(repo_url="owner/repo")
        d = dep.to_dict()
        assert "discovered_via" not in d
        assert "marketplace_plugin_name" not in d
        assert "source_url" not in d
        assert "source_digest" not in d

    def test_to_dict_includes_values(self):
        dep = LockedDependency(
            repo_url="owner/repo",
            discovered_via="acme-tools",
            marketplace_plugin_name="security-checks",
            source_url="https://catalog.example.com/marketplace.json",
            source_digest="sha256:" + "a" * 64,
        )
        d = dep.to_dict()
        assert d["discovered_via"] == "acme-tools"
        assert d["marketplace_plugin_name"] == "security-checks"
        source_url = urlparse(d["source_url"])
        assert (source_url.scheme, source_url.hostname, source_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert d["source_digest"] == "sha256:" + "a" * 64

    def test_from_dict_missing_fields(self):
        """Old lockfiles without provenance fields still deserialize."""
        dep = LockedDependency.from_dict({"repo_url": "owner/repo"})
        assert dep.discovered_via is None
        assert dep.marketplace_plugin_name is None

    def test_from_dict_with_fields(self):
        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
                "discovered_via": "acme-tools",
                "marketplace_plugin_name": "security-checks",
                "source_url": "https://catalog.example.com/marketplace.json",
                "source_digest": "sha256:" + "b" * 64,
            }
        )
        assert dep.discovered_via == "acme-tools"
        assert dep.marketplace_plugin_name == "security-checks"
        source_url = urlparse(dep.source_url)
        assert (source_url.scheme, source_url.hostname, source_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert dep.source_digest == "sha256:" + "b" * 64

    def test_roundtrip(self):
        original = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            resolved_ref="v1.0",
            discovered_via="acme-tools",
            marketplace_plugin_name="security-checks",
            source_url="https://catalog.example.com/marketplace.json",
            source_digest="sha256:" + "c" * 64,
        )
        restored = LockedDependency.from_dict(original.to_dict())
        assert restored.discovered_via == "acme-tools"
        assert restored.marketplace_plugin_name == "security-checks"
        source_url = urlparse(restored.source_url)
        assert (source_url.scheme, source_url.hostname, source_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert restored.source_digest == "sha256:" + "c" * 64
        assert restored.resolved_commit == "abc123"
        assert restored.resolved_ref == "v1.0"

    def test_backward_compat_existing_fields(self):
        """Ensure existing fields still work alongside new provenance fields."""
        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
                "resolved_commit": "abc123",
                "content_hash": "sha256:def456",
                "is_dev": True,
                "discovered_via": "mkt",
            }
        )
        assert dep.resolved_commit == "abc123"
        assert dep.content_hash == "sha256:def456"
        assert dep.is_dev is True
        assert dep.discovered_via == "mkt"

    def test_lockfile_builder_attaches_marketplace_source_provenance(self):
        lockfile = LockFile(
            dependencies={
                "owner/repo": LockedDependency(repo_url="owner/repo"),
            }
        )
        ctx = SimpleNamespace(
            marketplace_provenance={
                "owner/repo": {
                    "discovered_via": "catalog",
                    "marketplace_plugin_name": "tool",
                    "source_url": "https://catalog.example.com/marketplace.json",
                    "source_digest": "sha256:" + "f" * 64,
                }
            }
        )
        builder = LockfileBuilder(ctx)

        builder._attach_marketplace_provenance(lockfile)

        dep = lockfile.dependencies["owner/repo"]
        assert dep.discovered_via == "catalog"
        assert dep.marketplace_plugin_name == "tool"
        source_url = urlparse(dep.source_url)
        assert (source_url.scheme, source_url.hostname, source_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert dep.source_digest == "sha256:" + "f" * 64
