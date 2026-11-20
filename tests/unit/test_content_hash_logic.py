"""Tests for SHA-256 content integrity hashing (pure logic cases)."""

import os  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# compute_package_hash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verify_package_hash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# compute_file_hash (per-deployed-file provenance)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lockfile integration
# ---------------------------------------------------------------------------


class TestLockfileContentHash:
    def test_content_hash_serialized(self):
        """content_hash appears in lockfile YAML output."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency(
            repo_url="owner/repo",
            content_hash="sha256:abc123",
        )
        d = dep.to_dict()
        assert d["content_hash"] == "sha256:abc123"

    def test_content_hash_deserialized(self):
        """content_hash is read back from lockfile."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
                "content_hash": "sha256:abc123",
            }
        )
        assert dep.content_hash == "sha256:abc123"

    def test_missing_content_hash_backward_compat(self):
        """Old lockfiles without content_hash parse fine (None)."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
            }
        )
        assert dep.content_hash is None

    def test_content_hash_none_not_emitted(self):
        """content_hash=None is not written to YAML."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency(
            repo_url="owner/repo",
            content_hash=None,
        )
        d = dep.to_dict()
        assert "content_hash" not in d


# ---------------------------------------------------------------------------
# Lockfile content-hash streams (#4000): framed in version 4, legacy in 1-3
# ---------------------------------------------------------------------------
