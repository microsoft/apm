"""Source-aware drift rules for registry-sourced deps.

Covers the docs/proposals/registry-api.md §6.1 invariants:

- Manifest carries a semver range (e.g. ``^1.2.0``); lockfile carries an exact
  version (e.g. ``1.5.3``). Direct string comparison would be a false
  positive — drift is "did the locked version stop matching the manifest
  range?".
- Source-flip drift (git -> registry or vice versa) forces a re-resolve.
- ``build_download_ref`` replays the exact locked version for registry deps,
  not the manifest range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from apm_cli.drift import build_download_ref, detect_ref_change
from apm_cli.models.dependency.reference import DependencyReference


@dataclass
class _LockedDep:
    """Minimal LockedDependency stand-in for drift tests."""

    repo_url: str = "acme/web"
    resolved_ref: Optional[str] = None
    resolved_commit: Optional[str] = None
    host: Optional[str] = "github.com"
    registry_prefix: Optional[str] = None
    virtual_path: Optional[str] = None
    source: Optional[str] = None
    local_path: Optional[str] = None
    version: Optional[str] = None
    resolved_url: Optional[str] = None
    resolved_hash: Optional[str] = None
    is_insecure: bool = False
    allow_insecure: bool = False


class _LockfileStub:
    """Pretend lockfile that just exposes get_dependency for build_download_ref."""

    def __init__(self, deps_by_key):
        self._deps = deps_by_key

    def get_dependency(self, key):
        return self._deps.get(key)


# ───────────────────────────────────────────────────────────────────────────
# Source-flip drift
# ───────────────────────────────────────────────────────────────────────────


class TestSourceFlipDrift:
    def test_git_to_registry_is_drift(self):
        dep = DependencyReference(
            repo_url="acme/web", reference="^1.2.0", source="registry"
        )
        locked = _LockedDep(resolved_ref="v1.0", source=None)  # legacy git
        assert detect_ref_change(dep, locked) is True

    def test_registry_to_git_is_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v1.0")  # git
        locked = _LockedDep(source="registry", version="1.0.0")
        assert detect_ref_change(dep, locked) is True

    def test_legacy_none_source_equals_git(self):
        # Lockfile from before this PR has source=None for git deps.
        # A new manifest dep with source=None (Git default) MUST NOT be
        # treated as a source flip.
        dep = DependencyReference(repo_url="acme/web", reference="v1.0")
        locked = _LockedDep(resolved_ref="v1.0", source=None)
        assert detect_ref_change(dep, locked) is False

    def test_explicit_git_to_legacy_none_no_drift(self):
        # source="git" and source=None are equivalent.
        dep = DependencyReference(
            repo_url="acme/web", reference="v1.0", source="git"
        )
        locked = _LockedDep(resolved_ref="v1.0", source=None)
        assert detect_ref_change(dep, locked) is False


# ───────────────────────────────────────────────────────────────────────────
# Registry semver-range matching
# ───────────────────────────────────────────────────────────────────────────


class TestRegistryRangeMatching:
    def test_locked_version_inside_range_no_drift(self):
        dep = DependencyReference(
            repo_url="acme/web", reference="^1.2.0", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        assert detect_ref_change(dep, locked) is False

    def test_locked_version_outside_range_is_drift(self):
        # User tightened the range; locked version no longer matches.
        dep = DependencyReference(
            repo_url="acme/web", reference="~1.5.4", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        assert detect_ref_change(dep, locked) is True

    def test_user_expanded_range_to_include_locked_no_drift(self):
        # ``^1.0.0`` widens to include 1.5.3 — no drift, keep using it.
        dep = DependencyReference(
            repo_url="acme/web", reference="^1.0.0", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        assert detect_ref_change(dep, locked) is False

    def test_user_changed_to_major_bump_is_drift(self):
        dep = DependencyReference(
            repo_url="acme/web", reference="^2.0.0", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        assert detect_ref_change(dep, locked) is True

    def test_exact_version_match_no_drift(self):
        dep = DependencyReference(
            repo_url="acme/web", reference="1.5.3", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        assert detect_ref_change(dep, locked) is False

    def test_missing_version_in_lockfile_is_drift(self):
        # A registry dep with no `version` recorded is corrupt — fail loud.
        dep = DependencyReference(
            repo_url="acme/web", reference="^1.0.0", source="registry"
        )
        locked = _LockedDep(source="registry", version=None)
        assert detect_ref_change(dep, locked) is True

    def test_missing_manifest_ref_is_drift(self):
        dep = DependencyReference(
            repo_url="acme/web", reference=None, source="registry"
        )
        locked = _LockedDep(source="registry", version="1.0.0")
        assert detect_ref_change(dep, locked) is True

    def test_invalid_manifest_range_is_drift(self):
        # Branch-shaped ref slipped past parsing for some reason — fail
        # loud on the drift path so it surfaces.
        dep = DependencyReference(
            repo_url="acme/web", reference="main", source="registry"
        )
        locked = _LockedDep(source="registry", version="1.0.0")
        assert detect_ref_change(dep, locked) is True


# ───────────────────────────────────────────────────────────────────────────
# Backwards-compat: git deps unchanged
# ───────────────────────────────────────────────────────────────────────────


class TestGitDriftUnchanged:
    def test_same_ref_no_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v1.0")
        locked = _LockedDep(resolved_ref="v1.0")
        assert detect_ref_change(dep, locked) is False

    def test_changed_ref_is_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v2.0")
        locked = _LockedDep(resolved_ref="v1.0")
        assert detect_ref_change(dep, locked) is True

    def test_added_ref_is_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v2.0")
        locked = _LockedDep(resolved_ref=None)
        assert detect_ref_change(dep, locked) is True

    def test_removed_ref_is_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference=None)
        locked = _LockedDep(resolved_ref="v1.0")
        assert detect_ref_change(dep, locked) is True

    def test_update_refs_skips_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v2.0")
        locked = _LockedDep(resolved_ref="v1.0")
        assert detect_ref_change(dep, locked, update_refs=True) is False

    def test_no_locked_dep_is_first_install_not_drift(self):
        dep = DependencyReference(repo_url="acme/web", reference="v1.0")
        assert detect_ref_change(dep, None) is False


# ───────────────────────────────────────────────────────────────────────────
# build_download_ref for registry deps
# ───────────────────────────────────────────────────────────────────────────


class TestBuildDownloadRefRegistry:
    def test_registry_dep_replays_locked_version(self):
        # Manifest has ``^1.2.0`` (range), lockfile has ``1.5.3`` (exact).
        # The downloader should see ``1.5.3`` so the resolver picks the
        # exact locked version, not whatever ``^1.2.0`` resolves to today.
        dep = DependencyReference(
            repo_url="acme/web",
            reference="^1.2.0",
            source="registry",
            registry_name="corp",
        )
        locked = _LockedDep(
            source="registry",
            version="1.5.3",
            resolved_url="https://r/v1/.../download",
            resolved_hash="sha256:abc",
        )
        lockfile = _LockfileStub({"acme/web": locked})
        result = build_download_ref(
            dep, lockfile, update_refs=False, ref_changed=False
        )
        assert result.reference == "1.5.3"
        assert result.source == "registry"
        # registry_name is not in the override set; preserves manifest value
        assert result.registry_name == "corp"

    def test_registry_dep_with_ref_changed_uses_manifest_range(self):
        # The manifest range was tightened past the locked version. Drift
        # detection returns True; build_download_ref must NOT pin to the
        # locked version (it's now outside the range). Use the manifest.
        dep = DependencyReference(
            repo_url="acme/web",
            reference="~2.0.0",
            source="registry",
            registry_name="corp",
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        lockfile = _LockfileStub({"acme/web": locked})
        result = build_download_ref(
            dep, lockfile, update_refs=False, ref_changed=True
        )
        assert result.reference == "~2.0.0"

    def test_registry_dep_with_update_refs_uses_manifest_range(self):
        # ``apm install --update`` always re-resolves from the manifest.
        dep = DependencyReference(
            repo_url="acme/web",
            reference="^1.2.0",
            source="registry",
            registry_name="corp",
        )
        locked = _LockedDep(source="registry", version="1.5.3")
        lockfile = _LockfileStub({"acme/web": locked})
        result = build_download_ref(
            dep, lockfile, update_refs=True, ref_changed=False
        )
        assert result.reference == "^1.2.0"

    def test_git_dep_unchanged_uses_locked_commit(self):
        # Sanity: existing git path is byte-identical post-Phase-8.
        dep = DependencyReference(repo_url="acme/web", reference="v1.0")
        locked = _LockedDep(
            resolved_ref="v1.0", resolved_commit="abc123def4567890"
        )
        lockfile = _LockfileStub({"acme/web": locked})
        result = build_download_ref(
            dep, lockfile, update_refs=False, ref_changed=False
        )
        assert result.reference == "abc123def4567890"

    def test_no_locked_dep_returns_dep_unchanged(self):
        dep = DependencyReference(
            repo_url="acme/web",
            reference="^1.2.0",
            source="registry",
            registry_name="corp",
        )
        lockfile = _LockfileStub({})
        result = build_download_ref(
            dep, lockfile, update_refs=False, ref_changed=False
        )
        # No override applied — same identity, same range.
        assert result.reference == "^1.2.0"
        assert result.source == "registry"
