"""Tests for the APM lock file module."""

import pytest
from pathlib import Path
from unittest.mock import Mock
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.models.apm_package import DependencyReference


class TestLockedDependency:
    """Tests for LockedDependency dataclass."""

    def test_get_unique_key_regular(self):
        dep = LockedDependency(repo_url="owner/repo")
        assert dep.get_unique_key() == "owner/repo"

    def test_get_unique_key_virtual(self):
        dep = LockedDependency(repo_url="owner/repo", virtual_path="prompts/file.md", is_virtual=True)
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

    def test_mcp_servers_from_yaml(self):
        yaml_str = (
            'lockfile_version: "1"\n'
            'dependencies: []\n'
            'mcp_servers:\n'
            '  - github\n'
            '  - acme-kb\n'
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.mcp_servers == ["github", "acme-kb"]

    def test_read_nonexistent(self, tmp_path):
        loaded = LockFile.read(tmp_path / "apm.lock")
        assert loaded is None

    def test_from_installed_packages(self):
        dep_ref = Mock()
        dep_ref.repo_url = "owner/repo"
        dep_ref.host = "github.com"
        dep_ref.reference = "main"
        dep_ref.virtual_path = None
        dep_ref.is_virtual = False
        installed = [(dep_ref, "commit123", 1, None)]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.has_dependency("owner/repo")


class TestGetLockfilePath:
    def test_get_lockfile_path(self, tmp_path):
        path = get_lockfile_path(tmp_path)
        assert path == tmp_path / "apm.lock"
