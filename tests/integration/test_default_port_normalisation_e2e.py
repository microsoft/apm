"""End-to-end tests for #797 default-port normalisation.

Verifies that well-known default ports (443 for HTTPS, 80 for HTTP,
22 for SSH) are normalised away through the full dependency lifecycle:

    apm.yml  ->  DependencyReference.parse  ->  LockedDependency  ->  lockfile YAML

The contract: a URL with an explicit default port (e.g.
``https://github.com:443/owner/repo``) and the same URL without the
port MUST produce identical lockfile entries -- different spellings of
the same target must not create distinct lockfile keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_apm_yml(project: Path, deps: list[str]) -> Path:
    """Write a minimal apm.yml with the given string dependencies."""
    apm_yml = project / "apm.yml"
    config = {
        "name": "port-normalisation-test",
        "version": "1.0.0",
        "dependencies": {"apm": deps},
    }
    apm_yml.write_text(yaml.dump(config), encoding="utf-8")
    return apm_yml


def _parse_single_dep(project: Path, dep_str: str):
    """Write apm.yml with one dep, parse it, return the DependencyReference."""
    apm_yml = _write_apm_yml(project, [dep_str])
    pkg = APMPackage.from_apm_yml(apm_yml)
    deps = pkg.get_apm_dependencies()
    assert len(deps) == 1, f"Expected 1 dep, got {len(deps)}"
    return deps[0]


def _make_locked_dep(dep_ref) -> LockedDependency:
    """Create a LockedDependency from a DependencyReference with dummy resolution."""
    return LockedDependency.from_dependency_ref(
        dep_ref,
        resolved_commit="abc123def456",
        depth=1,
        resolved_by=None,
    )


class TestDefaultPortNormalisationE2E:
    """Issue #797: default ports normalised through the full dep lifecycle."""

    def test_https_443_normalised_through_apm_yml_parse(self, tmp_path):
        """https://github.com:443/owner/repo parsed via apm.yml drops port."""
        dep = _parse_single_dep(tmp_path, "https://github.com:443/owner/repo")
        assert dep.port is None
        assert dep.host == "github.com"
        assert dep.repo_url == "owner/repo"

    def test_ssh_22_normalised_through_apm_yml_parse(self, tmp_path):
        """ssh://git@gitlab.com:22/owner/repo.git parsed via apm.yml drops port."""
        dep = _parse_single_dep(tmp_path, "ssh://git@gitlab.com:22/owner/repo.git")
        assert dep.port is None
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "owner/repo"

    def test_non_default_port_preserved_through_apm_yml_parse(self, tmp_path):
        """Non-default port survives the full apm.yml parse path."""
        dep = _parse_single_dep(tmp_path, "https://bitbucket.corp.com:7990/team/repo")
        assert dep.port == 7990

    def test_lockfile_entry_omits_default_port(self, tmp_path):
        """LockedDependency created from a :443 URL has no port in its dict."""
        dep = _parse_single_dep(tmp_path, "https://github.com:443/owner/repo")
        locked = _make_locked_dep(dep)
        serialised = locked.to_dict()
        assert "port" not in serialised
        assert serialised["repo_url"] == "owner/repo"

    def test_lockfile_entry_preserves_non_default_port(self, tmp_path):
        """LockedDependency from a non-default port includes port in its dict."""
        dep = _parse_single_dep(tmp_path, "https://bitbucket.corp.com:7990/team/repo")
        locked = _make_locked_dep(dep)
        serialised = locked.to_dict()
        assert serialised["port"] == 7990

    def test_lockfile_key_identity_with_and_without_default_port(self, tmp_path):
        """Explicit :443 and bare URL produce the same lockfile key.

        This is the supply-chain correctness invariant: two spellings
        of the same target MUST NOT create separate lockfile entries.
        """
        project_a = tmp_path / "proj-a"
        project_a.mkdir()
        project_b = tmp_path / "proj-b"
        project_b.mkdir()

        dep_with_port = _parse_single_dep(project_a, "https://github.com:443/owner/repo")
        dep_bare = _parse_single_dep(project_b, "https://github.com/owner/repo")

        locked_with_port = _make_locked_dep(dep_with_port)
        locked_bare = _make_locked_dep(dep_bare)

        assert locked_with_port.get_unique_key() == locked_bare.get_unique_key()

    def test_lockfile_yaml_roundtrip_no_default_port(self, tmp_path):
        """Write lockfile with a :443 dep, read it back -- no port leaks.

        Exercises the full LockFile serialisation pipeline:
        parse -> LockedDependency -> LockFile.write -> LockFile.read.
        """
        dep = _parse_single_dep(tmp_path, "https://github.com:443/owner/repo")
        locked = _make_locked_dep(dep)

        lockfile = LockFile()
        lockfile.add_dependency(locked)

        lock_path = tmp_path / "apm.lock.yaml"
        lockfile.write(lock_path)

        # Read the raw YAML and verify no port key
        raw_yaml = lock_path.read_text(encoding="utf-8")
        assert "port:" not in raw_yaml
        assert ":443" not in raw_yaml

        # Roundtrip through LockFile.read
        loaded = LockFile.read(lock_path)
        assert loaded is not None
        reloaded_dep = loaded.dependencies.get("owner/repo")
        assert reloaded_dep is not None
        assert reloaded_dep.port is None

    def test_lockfile_yaml_roundtrip_preserves_non_default_port(self, tmp_path):
        """Non-default port survives the full lockfile write/read roundtrip."""
        dep = _parse_single_dep(tmp_path, "https://bitbucket.corp.com:7990/team/repo")
        locked = _make_locked_dep(dep)

        lockfile = LockFile()
        lockfile.add_dependency(locked)

        lock_path = tmp_path / "apm.lock.yaml"
        lockfile.write(lock_path)

        raw_yaml = lock_path.read_text(encoding="utf-8")
        assert "port: 7990" in raw_yaml

        loaded = LockFile.read(lock_path)
        assert loaded is not None
        reloaded_dep = loaded.dependencies.get("team/repo")
        assert reloaded_dep is not None
        assert reloaded_dep.port == 7990

    def test_canonical_string_identity_across_port_spellings(self, tmp_path):
        """to_canonical() output is identical for :443 and bare spellings."""
        project_a = tmp_path / "proj-c"
        project_a.mkdir()
        project_b = tmp_path / "proj-d"
        project_b.mkdir()

        dep_with_port = _parse_single_dep(project_a, "https://gitlab.com:443/acme/tools")
        dep_bare = _parse_single_dep(project_b, "https://gitlab.com/acme/tools")

        assert dep_with_port.to_canonical() == dep_bare.to_canonical()
        assert ":443" not in dep_with_port.to_canonical()

    def test_ssh_default_port_lockfile_roundtrip(self, tmp_path):
        """SSH with :22 produces a clean lockfile entry with no port."""
        dep = _parse_single_dep(tmp_path, "ssh://git@gitlab.com:22/acme/tools.git")
        locked = _make_locked_dep(dep)

        lockfile = LockFile()
        lockfile.add_dependency(locked)

        lock_path = tmp_path / "apm.lock.yaml"
        lockfile.write(lock_path)

        raw_yaml = lock_path.read_text(encoding="utf-8")
        assert "port:" not in raw_yaml

        loaded = LockFile.read(lock_path)
        reloaded = loaded.dependencies.get("acme/tools")
        assert reloaded is not None
        assert reloaded.port is None
