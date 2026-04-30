"""Tests for the APM lock file module."""

from pathlib import Path  # noqa: F401
from unittest.mock import Mock

import pytest  # noqa: F401
import yaml

from apm_cli.deps.lockfile import (
    LockedDependency,
    LockFile,
    get_lockfile_path,
    migrate_lockfile_if_needed,
)
from apm_cli.models.apm_package import DependencyReference


class TestLockedDependency:
    """Tests for LockedDependency dataclass."""

    def test_get_unique_key_regular(self):
        dep = LockedDependency(repo_url="owner/repo")
        assert dep.get_unique_key() == "owner/repo"

    def test_get_unique_key_virtual(self):
        dep = LockedDependency(
            repo_url="owner/repo", virtual_path="prompts/file.md", is_virtual=True
        )
        assert dep.get_unique_key() == "owner/repo/prompts/file.md"

    def test_to_dict_minimal(self):
        dep = LockedDependency(repo_url="owner/repo")
        result = dep.to_dict()
        assert result == {"repo_url": "owner/repo"}

    def test_from_dict(self):
        data = {"repo_url": "owner/repo", "host": "github.com", "depth": 2}
        dep = LockedDependency.from_dict(data)
        assert dep.repo_url == "owner/repo"
        assert dep.host == "github.com"

    def test_from_dependency_ref(self):
        dep_ref = DependencyReference(repo_url="owner/repo", host="github.com", reference="main")
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.repo_url == "owner/repo"
        assert locked.resolved_commit == "abc123"

    def test_port_round_trip_ssh(self):
        """Custom SSH port survives to_dict → from_dict."""
        dep = LockedDependency(
            repo_url="team/repo",
            host="bitbucket.example.com",
            port=7999,
        )
        data = dep.to_dict()
        assert data["port"] == 7999
        restored = LockedDependency.from_dict(data)
        assert restored.port == 7999
        assert restored.host == "bitbucket.example.com"

    def test_port_round_trip_https(self):
        """Custom HTTPS port survives to_dict → from_dict."""
        dep = LockedDependency(
            repo_url="team/repo",
            host="git.internal",
            port=8443,
        )
        data = dep.to_dict()
        assert data["port"] == 8443
        restored = LockedDependency.from_dict(data)
        assert restored.port == 8443

    def test_port_omitted_when_none(self):
        """port should not appear in the serialized dict when unset."""
        dep = LockedDependency(repo_url="owner/repo", host="github.com")
        data = dep.to_dict()
        assert "port" not in data

    def test_port_defensive_cast_invalid(self):
        """Garbage port values in a lockfile are rejected (defensive read)."""
        # Non-numeric string
        dep = LockedDependency.from_dict({"repo_url": "o/r", "port": "not-a-port"})
        assert dep.port is None
        # Out-of-range
        dep = LockedDependency.from_dict({"repo_url": "o/r", "port": 99999})
        assert dep.port is None
        dep = LockedDependency.from_dict({"repo_url": "o/r", "port": 0})
        assert dep.port is None
        dep = LockedDependency.from_dict({"repo_url": "o/r", "port": -1})
        assert dep.port is None

    def test_port_from_dependency_ref(self):
        """from_dependency_ref carries port through."""
        dep_ref = DependencyReference(
            repo_url="team/repo",
            host="bitbucket.example.com",
            port=7999,
        )
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.port == 7999

    def test_deployed_file_hashes_round_trip(self):
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=["a.md", "b.md"],
            deployed_file_hashes={"b.md": "sha256:dead", "a.md": "sha256:beef"},
        )
        d = dep.to_dict()
        # Serialised deterministically (sorted by key).
        assert list(d["deployed_file_hashes"].keys()) == ["a.md", "b.md"]
        assert LockedDependency.from_dict(d).deployed_file_hashes == dep.deployed_file_hashes

    def test_deployed_file_hashes_omitted_when_empty(self):
        """Backward compat: legacy dicts without the field stay clean."""
        dep = LockedDependency(repo_url="owner/repo")
        assert "deployed_file_hashes" not in dep.to_dict()

    def test_from_dict_missing_hashes_defaults_empty(self):
        loaded = LockedDependency.from_dict({"repo_url": "owner/repo"})
        assert loaded.deployed_file_hashes == {}


class TestLockFile:
    def test_add_and_get_dependency(self):
        lock = LockFile()
        dep = LockedDependency(repo_url="owner/repo", resolved_commit="abc123")
        lock.add_dependency(dep)
        assert lock.has_dependency("owner/repo")
        assert not lock.has_dependency("other/repo")

    def test_to_yaml(self):
        lock = LockFile(apm_version="1.0.0")
        lock.add_dependency(LockedDependency(repo_url="owner/repo"))
        yaml_str = lock.to_yaml()
        data = yaml.safe_load(yaml_str)
        assert data["lockfile_version"] == "1"
        assert len(data["dependencies"]) == 1

    def test_from_yaml(self):
        yaml_str = '\nlockfile_version: "1"\napm_version: "1.0.0"\ndependencies:\n  - repo_url: owner/repo\n'
        lock = LockFile.from_yaml(yaml_str)
        assert lock.apm_version == "1.0.0"
        assert lock.has_dependency("owner/repo")

    def test_write_and_read(self, tmp_path):
        lock = LockFile(apm_version="1.0.0")
        lock.add_dependency(LockedDependency(repo_url="owner/repo"))
        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)
        assert lock_path.exists()
        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.has_dependency("owner/repo")

    def test_mcp_servers_round_trip(self, tmp_path):
        """mcp_servers must survive a write → read cycle."""
        lock = LockFile(apm_version="1.0.0")
        lock.mcp_servers = ["github", "acme-kb", "atlassian"]
        lock.add_dependency(LockedDependency(repo_url="owner/repo"))
        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)
        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.mcp_servers == ["acme-kb", "atlassian", "github"]  # sorted

    def test_mcp_servers_empty_by_default(self):
        lock = LockFile()
        assert lock.mcp_servers == []
        yaml_str = lock.to_yaml()
        assert "mcp_servers" not in yaml_str  # omitted when empty

    def test_local_deployed_file_hashes_round_trip(self, tmp_path):
        """local_deployed_file_hashes must survive a write -> read cycle."""
        lock = LockFile()
        lock.local_deployed_files = ["a.md", "b.md"]
        lock.local_deployed_file_hashes = {"a.md": "sha256:1", "b.md": "sha256:2"}
        path = tmp_path / "apm.lock"
        lock.write(path)
        loaded = LockFile.read(path)
        assert loaded is not None
        assert loaded.local_deployed_file_hashes == {
            "a.md": "sha256:1",
            "b.md": "sha256:2",
        }

    def test_local_deployed_file_hashes_omitted_when_empty(self):
        lock = LockFile()
        assert "local_deployed_file_hashes" not in lock.to_yaml()

    def test_mcp_servers_from_yaml(self):
        yaml_str = (
            'lockfile_version: "1"\ndependencies: []\nmcp_servers:\n  - github\n  - acme-kb\n'
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.mcp_servers == ["github", "acme-kb"]

    def test_mcp_configs_round_trip(self, tmp_path):
        """mcp_configs survive a write/read cycle."""
        lock = LockFile()
        lock.mcp_configs = {
            "github": {"name": "github", "transport": "stdio"},
            "internal-kb": {
                "name": "internal-kb",
                "registry": False,
                "transport": "http",
                "url": "https://kb.example.com",
            },
        }
        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)

        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.mcp_configs == lock.mcp_configs

    def test_mcp_configs_empty_by_default(self):
        lock = LockFile()
        assert lock.mcp_configs == {}
        yaml_str = lock.to_yaml()
        assert "mcp_configs" not in yaml_str  # omitted when empty

    def test_mcp_configs_from_yaml(self):
        yaml_str = (
            'lockfile_version: "1"\n'
            "dependencies: []\n"
            "mcp_configs:\n"
            "  github:\n"
            "    name: github\n"
            "    transport: stdio\n"
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.mcp_configs == {"github": {"name": "github", "transport": "stdio"}}

    def test_mcp_configs_backward_compat_missing(self):
        """Old lockfiles without mcp_configs should get an empty dict."""
        yaml_str = 'lockfile_version: "1"\ndependencies: []\nmcp_servers:\n  - github\n'
        lock = LockFile.from_yaml(yaml_str)
        assert lock.mcp_servers == ["github"]
        assert lock.mcp_configs == {}

    def test_mcp_configs_backward_compat_null(self):
        """Lockfiles with mcp_configs: (null) should get an empty dict, not raise TypeError."""
        yaml_str = (
            'lockfile_version: "1"\n'
            "dependencies: []\n"
            "mcp_configs:\n"  # YAML null value
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.mcp_configs == {}

    def test_read_nonexistent(self, tmp_path):
        loaded = LockFile.read(tmp_path / "apm.lock.yaml")
        assert loaded is None

    def test_from_installed_packages(self):
        dep_ref = Mock()
        dep_ref.repo_url = "owner/repo"
        dep_ref.host = "github.com"
        dep_ref.reference = "main"
        dep_ref.virtual_path = None
        dep_ref.is_virtual = False
        dep_ref.is_local = False
        dep_ref.local_path = None
        installed = [(dep_ref, "commit123", 1, None)]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.has_dependency("owner/repo")


class TestGetLockfilePath:
    def test_get_lockfile_path(self, tmp_path):
        path = get_lockfile_path(tmp_path)
        assert path == tmp_path / "apm.lock.yaml"


class TestMigrateLockfileIfNeeded:
    def test_migrates_legacy_lockfile(self, tmp_path):
        """apm.lock should be renamed to apm.lock.yaml when new file is absent."""
        legacy = tmp_path / "apm.lock"
        legacy.write_text("lockfile_version: '1'\ndependencies: []\n")
        migrated = migrate_lockfile_if_needed(tmp_path)
        assert migrated is True
        assert not legacy.exists()
        assert (tmp_path / "apm.lock.yaml").exists()

    def test_no_migration_when_new_file_exists(self, tmp_path):
        """No migration when apm.lock.yaml already exists."""
        new_file = tmp_path / "apm.lock.yaml"
        new_file.write_text("lockfile_version: '1'\ndependencies: []\n")
        legacy = tmp_path / "apm.lock"
        legacy.write_text("old content")
        migrated = migrate_lockfile_if_needed(tmp_path)
        assert migrated is False
        assert legacy.exists()  # untouched
        assert new_file.read_text() == "lockfile_version: '1'\ndependencies: []\n"

    def test_no_migration_when_no_legacy_file(self, tmp_path):
        """Returns False when neither file exists."""
        migrated = migrate_lockfile_if_needed(tmp_path)
        assert migrated is False

    def test_migrated_file_is_readable(self, tmp_path):
        """Migrated lockfile can be loaded by LockFile.read."""
        lock = LockFile(apm_version="1.0.0")
        lock.add_dependency(LockedDependency(repo_url="owner/repo"))
        lock.write(tmp_path / "apm.lock")
        migrate_lockfile_if_needed(tmp_path)
        loaded = LockFile.read(tmp_path / "apm.lock.yaml")
        assert loaded is not None
        assert loaded.has_dependency("owner/repo")


class TestLockFileSemanticEquivalence:
    """Tests for LockFile.is_semantically_equivalent()."""

    def _make_lock(self, **overrides):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
            apm_version="0.8.5",
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                resolved_commit="abc123",
                depth=0,
            )
        )
        for k, v in overrides.items():
            setattr(lock, k, v)
        return lock

    def test_identical_is_equivalent(self):
        a = self._make_lock()
        b = self._make_lock()
        assert a.is_semantically_equivalent(b)

    def test_different_generated_at_still_equivalent(self):
        a = self._make_lock(generated_at="2025-01-01T00:00:00+00:00")
        b = self._make_lock(generated_at="2025-06-15T12:00:00+00:00")
        assert a.is_semantically_equivalent(b)

    def test_different_apm_version_still_equivalent(self):
        a = self._make_lock(apm_version="0.8.5")
        b = self._make_lock(apm_version="0.9.0")
        assert a.is_semantically_equivalent(b)

    def test_added_dependency_not_equivalent(self):
        a = self._make_lock()
        b = self._make_lock()
        b.add_dependency(LockedDependency(repo_url="other/pkg", depth=1))
        assert not a.is_semantically_equivalent(b)

    def test_removed_dependency_not_equivalent(self):
        a = self._make_lock()
        b = LockFile()
        assert not a.is_semantically_equivalent(b)

    def test_changed_mcp_servers_not_equivalent(self):
        a = self._make_lock(mcp_servers=["server-a"])
        b = self._make_lock(mcp_servers=["server-b"])
        assert not a.is_semantically_equivalent(b)

    def test_mcp_server_order_irrelevant(self):
        a = self._make_lock(mcp_servers=["b", "a"])
        b = self._make_lock(mcp_servers=["a", "b"])
        assert a.is_semantically_equivalent(b)

    def test_changed_mcp_configs_not_equivalent(self):
        a = self._make_lock(mcp_configs={"s": {"cmd": "a"}})
        b = self._make_lock(mcp_configs={"s": {"cmd": "b"}})
        assert not a.is_semantically_equivalent(b)

    def test_changed_lockfile_version_not_equivalent(self):
        a = self._make_lock(lockfile_version="1")
        b = self._make_lock(lockfile_version="2")
        assert not a.is_semantically_equivalent(b)

    def test_new_lockfile_vs_empty(self):
        a = self._make_lock()
        b = LockFile()
        assert not a.is_semantically_equivalent(b)
