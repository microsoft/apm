"""Tests for the lockfile v1->v2 opportunistic bump.

Covers docs/proposals/registry-api.md §6.1 (registry resolver fields:
``resolved_url``, ``resolved_hash``) and the invariant that a project that
never uses the registry keeps lockfile_version "1" forever (§2.1.4).
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.resolver import RegistryResolution
from apm_cli.models.dependency.reference import DependencyReference


def _git_dep(**kwargs) -> LockedDependency:
    defaults = dict(
        repo_url="acme/foo",
        host="github.com",
        resolved_commit="abc123",
        resolved_ref="v1.0",
    )
    defaults.update(kwargs)
    return LockedDependency(**defaults)


def _registry_dep(**kwargs) -> LockedDependency:
    defaults = dict(
        repo_url="acme/web",
        host="github.com",
        source="registry",
        version="1.2.0",
        resolved_url=(
            "https://reg.example.com/apm/v1/packages/acme/web/versions/1.2.0/tarball"
        ),
        resolved_hash="sha256:" + "a" * 64,
    )
    defaults.update(kwargs)
    return LockedDependency(**defaults)


# ───────────────────────────────────────────────────────────────────────────
# Schema bump (invariant §2.1.4)
# ───────────────────────────────────────────────────────────────────────────


class TestVersionFieldStaysInSync:
    """Regression: ``lockfile_version`` must reflect content in the in-memory
    object, not just at to_yaml() emit time. Otherwise the equivalence check
    sees ``"1"`` in-memory vs ``"2"`` on-disk and rewrites the file on every
    no-op install.
    """

    def test_add_registry_dep_promotes_field_to_v2(self):
        lock = LockFile()  # default "1"
        assert lock.lockfile_version == "1"
        lock.add_dependency(_registry_dep())
        assert lock.lockfile_version == "2"

    def test_add_git_dep_keeps_field_at_v1(self):
        lock = LockFile()
        lock.add_dependency(_git_dep())
        assert lock.lockfile_version == "1"

    def test_inmem_and_disk_equivalent_after_add(self):
        in_memory = LockFile()
        in_memory.add_dependency(_registry_dep())
        on_disk = LockFile.from_yaml(in_memory.to_yaml())
        assert in_memory.lockfile_version == on_disk.lockfile_version == "2"
        assert in_memory.is_semantically_equivalent(on_disk)
        assert on_disk.is_semantically_equivalent(in_memory)

    def test_to_yaml_self_heals_direct_dict_mutation(self):
        # Defense-in-depth: a caller that bypasses add_dependency by
        # mutating ``self.dependencies`` directly will still get the
        # right emit version on first to_yaml() call.
        lock = LockFile()
        lock.dependencies[_registry_dep().get_unique_key()] = _registry_dep()
        assert lock.lockfile_version == "1"  # bypass — field stale
        out = lock.to_yaml()
        assert lock.lockfile_version == "2"  # to_yaml self-healed
        assert "lockfile_version: '2'" in out


class TestSchemaBumpOpportunistic:
    def test_v1_stays_v1_without_registry_deps(self):
        lock = LockFile()
        lock.add_dependency(_git_dep())
        yaml_out = lock.to_yaml()
        assert "lockfile_version: '1'" in yaml_out

    def test_v2_emitted_when_registry_dep_present(self):
        lock = LockFile()
        lock.add_dependency(_git_dep())
        lock.add_dependency(_registry_dep())
        yaml_out = lock.to_yaml()
        assert "lockfile_version: '2'" in yaml_out

    def test_only_local_deps_stays_v1(self):
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/x", source="local", local_path="./local/x"
            )
        )
        assert "lockfile_version: '1'" in lock.to_yaml()

    def test_v2_back_to_v1_when_registry_dep_removed(self):
        lock = LockFile()
        lock.add_dependency(_registry_dep())
        assert "lockfile_version: '2'" in lock.to_yaml()
        # Remove the registry dep; lockfile re-emits as v1.
        del lock.dependencies["acme/web"]
        lock.add_dependency(_git_dep())
        assert "lockfile_version: '1'" in lock.to_yaml()


# ───────────────────────────────────────────────────────────────────────────
# Field round-trip
# ───────────────────────────────────────────────────────────────────────────


class TestRegistryFieldsRoundTrip:
    def test_resolved_url_and_hash_emitted(self):
        lock = LockFile()
        lock.add_dependency(_registry_dep())
        yaml_out = lock.to_yaml()
        assert "resolved_url" in yaml_out
        assert "resolved_hash" in yaml_out

    def test_resolved_url_and_hash_omitted_for_git_deps(self):
        lock = LockFile()
        lock.add_dependency(_git_dep())
        yaml_out = lock.to_yaml()
        assert "resolved_url" not in yaml_out
        assert "resolved_hash" not in yaml_out

    def test_round_trip_preserves_registry_fields(self):
        lock = LockFile()
        lock.add_dependency(_registry_dep())
        out = LockFile.from_yaml(lock.to_yaml())
        reg = out.get_dependency("acme/web")
        assert reg.source == "registry"
        assert reg.resolved_url.endswith("/tarball")
        assert reg.resolved_hash.startswith("sha256:")
        assert reg.version == "1.2.0"


# ───────────────────────────────────────────────────────────────────────────
# v1 lockfile compatibility (§2.1.4 read invariant)
# ───────────────────────────────────────────────────────────────────────────


class TestV1ReadCompat:
    def test_v1_lockfile_parses_without_migration(self):
        v1_yaml = (
            "lockfile_version: '1'\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "dependencies:\n"
            "- repo_url: acme/old\n"
            "  resolved_commit: abc123\n"
            "  resolved_ref: v1.0\n"
            "  host: github.com\n"
        )
        lock = LockFile.from_yaml(v1_yaml)
        old = lock.get_dependency("acme/old")
        assert old.resolved_url is None
        assert old.resolved_hash is None
        assert old.source is None

    def test_v1_lockfile_reemits_as_v1(self):
        v1_yaml = (
            "lockfile_version: '1'\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "dependencies:\n"
            "- repo_url: acme/old\n"
            "  resolved_commit: abc123\n"
            "  host: github.com\n"
        )
        lock = LockFile.from_yaml(v1_yaml)
        out = lock.to_yaml()
        assert "lockfile_version: '1'" in out

    def test_v2_lockfile_with_registry_field_parses(self):
        v2_yaml = (
            "lockfile_version: '2'\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "dependencies:\n"
            "- repo_url: acme/web\n"
            "  source: registry\n"
            "  version: 1.2.0\n"
            "  resolved_url: https://r/tarball\n"
            "  resolved_hash: sha256:abc\n"
            "  host: github.com\n"
        )
        lock = LockFile.from_yaml(v2_yaml)
        reg = lock.get_dependency("acme/web")
        assert reg.source == "registry"
        assert reg.resolved_url == "https://r/tarball"


# ───────────────────────────────────────────────────────────────────────────
# from_dependency_ref + RegistryResolution
# ───────────────────────────────────────────────────────────────────────────


class TestFromDependencyRefWithResolution:
    def test_registry_resolution_populates_fields(self):
        dep = DependencyReference(
            repo_url="acme/x",
            reference="^1.0.0",
            source="registry",
            registry_name="corp",
        )
        resolution = RegistryResolution(
            resolved_url="https://r/v1/packages/acme/x/versions/1.5.0/tarball",
            resolved_hash="sha256:def",
            version="1.5.0",
        )
        locked = LockedDependency.from_dependency_ref(
            dep,
            resolved_commit=None,
            depth=1,
            resolved_by=None,
            registry_resolution=resolution,
        )
        assert locked.source == "registry"
        assert locked.resolved_url == resolution.resolved_url
        assert locked.resolved_hash == "sha256:def"
        assert locked.version == "1.5.0"

    def test_no_resolution_means_no_registry_fields(self):
        # Existing git-resolver call site (no registry_resolution arg)
        # produces a lockfile entry with source=None and no registry fields.
        dep = DependencyReference(repo_url="acme/x", reference="v1.0")
        locked = LockedDependency.from_dependency_ref(
            dep, resolved_commit="abc", depth=1, resolved_by=None
        )
        assert locked.source is None
        assert locked.resolved_url is None
        assert locked.resolved_hash is None

    def test_local_dep_unchanged(self):
        dep = DependencyReference(
            repo_url="_local/pkg", is_local=True, local_path="./pkg"
        )
        locked = LockedDependency.from_dependency_ref(
            dep, resolved_commit=None, depth=1, resolved_by=None
        )
        assert locked.source == "local"
        assert locked.local_path == "./pkg"


# ───────────────────────────────────────────────────────────────────────────
# to_dependency_ref restores source field
# ───────────────────────────────────────────────────────────────────────────


class TestToDependencyRefRestoresSource:
    def test_registry_source_restored(self):
        locked = _registry_dep()
        ref = locked.to_dependency_ref()
        assert ref.source == "registry"

    def test_registry_uses_locked_version_not_range(self):
        # If a registry dep was originally pinned with ``^1.2.0`` and resolved
        # to 1.5.3, the lockfile records 1.5.3 in `version`. to_dependency_ref
        # must surface the exact version so re-installs are reproducible.
        locked = _registry_dep(version="1.5.3", resolved_ref="^1.2.0")
        ref = locked.to_dependency_ref()
        assert ref.reference == "1.5.3"

    def test_git_source_restored_as_none(self):
        locked = _git_dep()
        ref = locked.to_dependency_ref()
        assert ref.source is None

    def test_local_source_restored(self):
        locked = LockedDependency(
            repo_url="_local/x", source="local", local_path="./x"
        )
        ref = locked.to_dependency_ref()
        assert ref.source == "local"
        assert ref.is_local
