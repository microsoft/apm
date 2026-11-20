"""Unit tests for the local .apm/ content integration feature in apm install (pure logic cases)."""

import pytest

from apm_cli.deps.lockfile import LockFile

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: _has_local_apm_content()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: _integrate_local_content()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: LockFile.local_deployed_files
# ---------------------------------------------------------------------------


class TestLockFileLocalDeployedFiles:
    """Tests for the LockFile.local_deployed_files field."""

    def test_lockfile_local_deployed_files_default_empty(self):
        """A freshly created LockFile has an empty local_deployed_files list."""
        lock = LockFile()
        assert lock.local_deployed_files == []

    def test_lockfile_from_yaml_missing_key_defaults_to_empty(self):
        """from_yaml with no local_deployed_files key defaults to empty list."""
        import yaml

        raw = {
            "lockfile_version": "1",
            "generated_at": "2024-01-01T00:00:00+00:00",
            "dependencies": [],
        }
        lock = LockFile.from_yaml(yaml.dump(raw))
        assert lock.local_deployed_files == []

    def test_lockfile_semantic_equivalence_order_independent(self):
        """Semantic equivalence is order-independent for local_deployed_files."""
        lock_a = LockFile()
        lock_a.local_deployed_files = ["b.md", "a.md"]

        lock_b = LockFile()
        lock_b.local_deployed_files = ["a.md", "b.md"]

        assert lock_a.is_semantically_equivalent(lock_b)
