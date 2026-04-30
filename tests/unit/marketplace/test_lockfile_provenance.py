"""Tests for lockfile provenance fields -- serialization round-trip and backward compat."""

import pytest  # noqa: F401

from apm_cli.deps.lockfile import LockedDependency


class TestLockedDependencyProvenance:
    """Verify marketplace provenance fields round-trip correctly."""

    def test_default_none(self):
        dep = LockedDependency(repo_url="owner/repo")
        assert dep.discovered_via is None
        assert dep.marketplace_plugin_name is None

    def test_to_dict_omits_none(self):
        dep = LockedDependency(repo_url="owner/repo")
        d = dep.to_dict()
        assert "discovered_via" not in d
        assert "marketplace_plugin_name" not in d

    def test_to_dict_includes_values(self):
        dep = LockedDependency(
            repo_url="owner/repo",
            discovered_via="acme-tools",
            marketplace_plugin_name="security-checks",
        )
        d = dep.to_dict()
        assert d["discovered_via"] == "acme-tools"
        assert d["marketplace_plugin_name"] == "security-checks"

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
            }
        )
        assert dep.discovered_via == "acme-tools"
        assert dep.marketplace_plugin_name == "security-checks"

    def test_roundtrip(self):
        original = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            resolved_ref="v1.0",
            discovered_via="acme-tools",
            marketplace_plugin_name="security-checks",
        )
        restored = LockedDependency.from_dict(original.to_dict())
        assert restored.discovered_via == "acme-tools"
        assert restored.marketplace_plugin_name == "security-checks"
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
