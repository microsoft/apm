"""Tests for the APM lock file module."""

from pathlib import Path  # noqa: F401
from unittest.mock import Mock

import pytest
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

    def test_get_unique_key_preserves_github_default_host(self):
        dep = LockedDependency(repo_url="owner/repo", host="github.com")
        assert dep.get_unique_key() == "owner/repo"

    def test_get_unique_key_includes_non_default_host(self):
        dep = LockedDependency(repo_url="team/skills", host="gitea.myorg.com")
        assert dep.get_unique_key() == "gitea.myorg.com/team/skills"

    def test_get_unique_key_lowercases_non_default_host(self):
        mixed_case = LockedDependency(repo_url="team/skills", host="Gitea.MyOrg.com")
        lower_case = LockedDependency(repo_url="team/skills", host="gitea.myorg.com")

        assert mixed_case.get_unique_key() == lower_case.get_unique_key()
        assert lower_case.get_unique_key() == "gitea.myorg.com/team/skills"

    def test_get_unique_key_includes_non_default_host_for_virtual_dep(self):
        dep = LockedDependency(
            repo_url="team/skills",
            host="git.internal.example.com",
            virtual_path="prompts/review.prompt.md",
            is_virtual=True,
        )
        assert (
            dep.get_unique_key() == "git.internal.example.com/team/skills/prompts/review.prompt.md"
        )

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

    def test_host_type_round_trip(self):
        dep = LockedDependency(
            repo_url="team/repo",
            host="code.acme.com",
            host_type="gitlab",
        )
        data = dep.to_dict()
        assert data["host_type"] == "gitlab"
        restored = LockedDependency.from_dict(data)
        assert restored.host_type == "gitlab"
        assert restored.to_dependency_ref().host_type == "gitlab"

    def test_rejects_unknown_host_type_from_lockfile(self):
        with pytest.raises(ValueError, match="Supported values: gitlab"):
            LockedDependency.from_dict(
                {"repo_url": "team/repo", "host": "code.acme.com", "host_type": "gitea"}
            )

    def test_host_type_from_dependency_ref(self):
        dep_ref = DependencyReference(
            repo_url="team/repo",
            host="code.acme.com",
            host_type="gitlab",
        )
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.host_type == "gitlab"

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

    def test_deployed_files_deduplicated_when_serialized_after_repeated_update(self, tmp_path):
        """Repeated installs may append the same path; lock output stays canonical."""
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                deployed_files=["b.md", "a.md", "b.md", "a.md"],
            )
        )

        assert lock.get_dependency("owner/repo").deployed_files == ["b.md", "a.md"]

        lock_path = tmp_path / "apm.lock.yaml"
        lock.write(lock_path)

        data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
        [dep_data] = data["dependencies"]
        assert dep_data["deployed_files"] == ["a.md", "b.md"]

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

    def test_add_dependency_keeps_same_repo_from_different_hosts(self):
        lock = LockFile()
        lock.add_dependency(LockedDependency(repo_url="team/skills", host="github.com"))
        lock.add_dependency(LockedDependency(repo_url="team/skills", host="gitea.myorg.com"))

        assert set(lock.dependencies) == {"team/skills", "gitea.myorg.com/team/skills"}
        assert lock.get_dependency("team/skills").host == "github.com"
        assert lock.get_dependency("gitea.myorg.com/team/skills").host == "gitea.myorg.com"

    def test_dependency_reference_key_includes_non_default_host(self):
        dep = DependencyReference.parse("gitea.myorg.com/team/skills")
        assert dep.get_unique_key() == "gitea.myorg.com/team/skills"

    def test_dependency_reference_key_preserves_github_com_default(self):
        dep = DependencyReference.parse("github.com/team/skills")
        assert dep.get_unique_key() == "team/skills"

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

    def test_lsp_servers_round_trip(self, tmp_path):
        """lsp_servers must survive a write -> read cycle."""
        lock = LockFile(apm_version="1.0.0")
        lock.lsp_servers = ["pyright", "ruff-lsp"]
        lock.add_dependency(LockedDependency(repo_url="owner/repo"))
        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)
        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.lsp_servers == ["pyright", "ruff-lsp"]

    def test_lsp_servers_empty_by_default(self):
        lock = LockFile()
        assert lock.lsp_servers == []
        yaml_str = lock.to_yaml()
        assert "lsp_servers" not in yaml_str

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

    def test_lsp_configs_round_trip(self, tmp_path):
        """lsp_configs survive a write/read cycle."""
        lock = LockFile()
        lock.lsp_configs = {
            "pyright": {
                "name": "pyright",
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            }
        }
        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)

        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.lsp_configs == lock.lsp_configs

    def test_lsp_configs_empty_by_default(self):
        lock = LockFile()
        assert lock.lsp_configs == {}
        yaml_str = lock.to_yaml()
        assert "lsp_configs" not in yaml_str

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

    def test_changed_lsp_servers_not_equivalent(self):
        a = self._make_lock(lsp_servers=["server-a"])
        b = self._make_lock(lsp_servers=["server-b"])
        assert not a.is_semantically_equivalent(b)

    def test_lsp_server_order_irrelevant(self):
        a = self._make_lock(lsp_servers=["b", "a"])
        b = self._make_lock(lsp_servers=["a", "b"])
        assert a.is_semantically_equivalent(b)

    def test_changed_lsp_configs_not_equivalent(self):
        a = self._make_lock(lsp_configs={"s": {"cmd": "a"}})
        b = self._make_lock(lsp_configs={"s": {"cmd": "b"}})
        assert not a.is_semantically_equivalent(b)

    def test_changed_lockfile_version_not_equivalent(self):
        a = self._make_lock(lockfile_version="1")
        b = self._make_lock(lockfile_version="2")
        assert not a.is_semantically_equivalent(b)

    def test_new_lockfile_vs_empty(self):
        a = self._make_lock()
        b = LockFile()
        assert not a.is_semantically_equivalent(b)


# ---------------------------------------------------------------------------
# Issue #1888: installed package name + version per lockfile entry
# ---------------------------------------------------------------------------


class TestLockedDependencyPkgMetadata:
    """Tests for the name / version fields added by issue #1888 (Option A).

    Blocking tests a, b, e, f, g from the advisory panel.
    """

    # --- (a) round-trip ----------------------------------------------------

    def test_to_dict_emits_name_when_set(self):
        dep = LockedDependency(repo_url="owner/repo", name="my-package")
        d = dep.to_dict()
        assert d["name"] == "my-package"

    def test_to_dict_omits_name_when_none(self):
        dep = LockedDependency(repo_url="owner/repo", name=None)
        d = dep.to_dict()
        assert "name" not in d

    def test_from_dict_restores_name(self):
        d = {"repo_url": "owner/repo", "name": "my-package", "resolved_commit": "abc"}
        dep = LockedDependency.from_dict(d)
        assert dep.name == "my-package"

    def test_round_trip_name_and_version(self):
        dep = LockedDependency(
            repo_url="owner/repo",
            name="my-package",
            version="1.2.3",
            resolved_commit="abc123",
        )
        restored = LockedDependency.from_dict(dep.to_dict())
        assert restored.name == "my-package"
        assert restored.version == "1.2.3"

    def test_round_trip_name_none_omitted_not_null(self):
        dep = LockedDependency(repo_url="owner/repo", name=None)
        d = dep.to_dict()
        assert "name" not in d
        restored = LockedDependency.from_dict(d)
        assert restored.name is None

    def test_round_trip_empty_name_preserved(self):
        dep = LockedDependency(repo_url="owner/repo", name="")
        d = dep.to_dict()
        assert d["name"] == ""
        restored = LockedDependency.from_dict(d)
        assert restored.name == ""

    # --- (g) _known_keys includes "name" -----------------------------------

    def test_name_in_known_keys_not_unknown_fields(self):
        """'name' must be in _known_keys so it lands on .name, not _unknown_fields."""
        d = {"repo_url": "owner/repo", "name": "pkg-name"}
        dep = LockedDependency.from_dict(d)
        assert dep.name == "pkg-name"
        assert "name" not in dep._unknown_fields

    # --- (b) from_dependency_ref priority ----------------------------------

    def test_from_dependency_ref_sets_name_from_package_name(self):
        dep_ref = DependencyReference(repo_url="owner/repo", reference="main")
        locked = LockedDependency.from_dependency_ref(
            dep_ref, "abc123", 1, None, package_name="cool-pkg"
        )
        assert locked.name == "cool-pkg"

    def test_from_dependency_ref_package_version_fallback_no_resolution(self):
        """package_version is used for version when no registry/semver resolution present."""
        dep_ref = DependencyReference(repo_url="owner/repo", reference="main")
        locked = LockedDependency.from_dependency_ref(
            dep_ref, "abc123", 1, None, package_version="2.3.4"
        )
        assert locked.version == "2.3.4"

    def test_from_dependency_ref_registry_resolution_wins_over_package_version(self):
        """registry_resolution.version must win over package_version (Option A)."""
        from apm_cli.deps.registry.resolver import RegistryResolution

        dep_ref = DependencyReference(repo_url="owner/repo", reference="1.0.0")
        reg_res = RegistryResolution(
            version="3.0.0",
            resolved_url="https://reg.example.com/pkg/3.0.0",
            resolved_hash="sha256:" + "a" * 64,
        )
        locked = LockedDependency.from_dependency_ref(
            dep_ref,
            "abc123",
            1,
            None,
            registry_resolution=reg_res,
            package_version="9.9.9",
        )
        assert locked.version == "3.0.0"

    def test_from_dependency_ref_git_semver_resolution_wins_over_package_version(self):
        from apm_cli.deps.git_semver_resolver import GitSemverResolution

        dep_ref = DependencyReference(repo_url="owner/repo", reference="^1.0.0")
        git_semver_resolution = GitSemverResolution(
            constraint="^1.0.0",
            resolved_version="1.2.3",
            resolved_tag="v1.2.3",
            resolved_sha="a" * 40,
            matched_pattern="v{version}",
            resolved_at="2026-06-25T00:00:00Z",
        )
        locked = LockedDependency.from_dependency_ref(
            dep_ref,
            "abc123",
            1,
            None,
            git_semver_resolution=git_semver_resolution,
            package_version="9.9.9",
        )
        assert locked.version == "1.2.3"

    def test_from_dependency_ref_no_name_when_not_provided(self):
        dep_ref = DependencyReference(repo_url="owner/repo", reference="main")
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.name is None

    # --- (e) identity-key invariance ---------------------------------------

    def test_get_unique_key_invariant_git(self):
        """name/version must NOT affect get_unique_key() for git deps."""
        base = LockedDependency(repo_url="owner/repo", resolved_commit="abc123")
        with_meta = LockedDependency(
            repo_url="owner/repo", resolved_commit="abc123", name="pkg", version="1.0.0"
        )
        assert base.get_unique_key() == with_meta.get_unique_key()

    def test_get_unique_key_invariant_local(self):
        base = LockedDependency(repo_url="pkg", source="local", local_path="./pkg")
        with_meta = LockedDependency(
            repo_url="pkg", source="local", local_path="./pkg", name="pkg", version="0.1.0"
        )
        assert base.get_unique_key() == with_meta.get_unique_key()

    def test_get_unique_key_invariant_registry(self):
        base = LockedDependency(repo_url="owner/repo", source="registry", version="1.0.0")
        with_meta = LockedDependency(
            repo_url="owner/repo", source="registry", version="1.0.0", name="owner-repo"
        )
        assert base.get_unique_key() == with_meta.get_unique_key()

    # --- (f) to_dependency_ref() ignores name, is_registry guard unchanged -

    def test_to_dependency_ref_git_uses_resolved_ref_not_version(self):
        """For a git dep, version is apm.yml metadata; resolved_ref drives replay."""
        locked = LockedDependency(
            repo_url="owner/repo",
            source=None,
            resolved_ref="main",
            resolved_commit="abc123",
            version="1.0.0",
            name="some-pkg",
        )
        ref = locked.to_dependency_ref()
        assert ref.reference == "main"

    def test_to_dependency_ref_registry_uses_version_as_ref(self):
        """For registry deps, version IS the replay selector."""
        locked = LockedDependency(
            repo_url="owner/repo",
            source="registry",
            version="2.5.0",
            name="some-pkg",
        )
        ref = locked.to_dependency_ref()
        assert ref.reference == "2.5.0"

    def test_to_dependency_ref_name_field_not_on_ref(self):
        """DependencyReference has no 'name' attribute; name is display-only."""
        locked = LockedDependency(
            repo_url="owner/repo",
            source=None,
            resolved_ref="main",
            name="some-pkg",
        )
        ref = locked.to_dependency_ref()
        assert not hasattr(ref, "name")


class TestFromInstalledPackagesPkgMetadata:
    """Test (c): from_installed_packages threads package_name/package_version through."""

    def test_installed_package_with_pkg_metadata_yields_name_and_version(self):
        from apm_cli.deps.installed_package import InstalledPackage

        dep_ref = DependencyReference(repo_url="owner/foo", reference="main")
        pkg = InstalledPackage(
            dep_ref=dep_ref,
            resolved_commit="abc123",
            depth=1,
            resolved_by=None,
            is_dev=False,
            package_name="foo",
            package_version="2.0.0",
        )

        graph_mock = Mock()
        node_mock = Mock()
        node_mock.depth = 1
        node_mock.parent = None
        node_mock.is_dev = False
        graph_mock.dependency_tree.get_node.return_value = node_mock

        lock = LockFile.from_installed_packages([pkg], graph_mock)
        dep = lock.get_dependency("owner/foo")
        assert dep is not None
        assert dep.name == "foo"
        assert dep.version == "2.0.0"

    def test_installed_package_without_pkg_metadata_leaves_name_none(self):
        from apm_cli.deps.installed_package import InstalledPackage

        dep_ref = DependencyReference(repo_url="owner/bar", reference="v1")
        pkg = InstalledPackage(
            dep_ref=dep_ref,
            resolved_commit="def456",
            depth=1,
            resolved_by=None,
        )

        graph_mock = Mock()
        node_mock = Mock()
        node_mock.depth = 1
        node_mock.parent = None
        node_mock.is_dev = False
        graph_mock.dependency_tree.get_node.return_value = node_mock

        lock = LockFile.from_installed_packages([pkg], graph_mock)
        dep = lock.get_dependency("owner/bar")
        assert dep is not None
        assert dep.name is None
