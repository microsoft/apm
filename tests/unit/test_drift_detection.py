"""Unit tests for apm_cli.drift -- pure drift-detection helpers.

These tests cover detect_ref_change, detect_orphans, detect_config_drift,
and build_download_ref.  All four functions are stateless and side-effect-free
so no I/O or network access is required.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import MagicMock  # noqa: F401

from apm_cli.drift import (
    build_download_ref,
    detect_config_drift,
    detect_orphans,
    detect_ref_change,
)
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Helpers: minimal LockedDependency stub
# ---------------------------------------------------------------------------


@dataclass
class _LockedDep:
    """Minimal stand-in for LockedDependency to keep tests self-contained."""

    repo_url: str = "owner/repo"
    resolved_ref: str | None = None
    resolved_commit: str | None = None
    host: str | None = None
    registry_prefix: str | None = None
    virtual_path: str | None = None
    source: str | None = None
    local_path: str | None = None
    deployed_files: list[str] = field(default_factory=list)

    def get_unique_key(self) -> str:
        if self.source == "local" and self.local_path:
            return self.local_path
        if self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url


@dataclass
class _LockFile:
    """Minimal stand-in for LockFile."""

    dependencies: dict[str, _LockedDep] = field(default_factory=dict)

    def get_dependency(self, key: str) -> _LockedDep | None:
        return self.dependencies.get(key)


def _dep(repo_url: str = "owner/repo", reference: str | None = None) -> DependencyReference:
    return DependencyReference(repo_url=repo_url, reference=reference)


# ---------------------------------------------------------------------------
# detect_ref_change
# ---------------------------------------------------------------------------


class TestDetectRefChange(unittest.TestCase):
    """Tests for detect_ref_change()."""

    # --- update_refs mode ---

    def test_update_refs_always_false(self):
        """update_refs=True means we intentionally ignore the lockfile."""
        locked = _LockedDep(resolved_ref="v1.0.0")
        dep = _dep(reference="v2.0.0")
        self.assertFalse(detect_ref_change(dep, locked, update_refs=True))

    def test_update_refs_false_no_lockfile(self):
        """update_refs=True with no locked entry still returns False."""
        dep = _dep(reference="v1.0.0")
        self.assertFalse(detect_ref_change(dep, None, update_refs=True))

    # --- new package (locked_dep is None) ---

    def test_new_package_returns_false(self):
        """Brand-new package is not considered drift."""
        dep = _dep(reference="v1.0.0")
        self.assertFalse(detect_ref_change(dep, None))

    def test_new_package_no_ref_returns_false(self):
        dep = _dep()
        self.assertFalse(detect_ref_change(dep, None))

    # --- ref unchanged ---

    def test_same_ref_returns_false(self):
        locked = _LockedDep(resolved_ref="v1.0.0")
        dep = _dep(reference="v1.0.0")
        self.assertFalse(detect_ref_change(dep, locked))

    def test_both_none_returns_false(self):
        locked = _LockedDep(resolved_ref=None)
        dep = _dep(reference=None)
        self.assertFalse(detect_ref_change(dep, locked))

    # --- ref changed ---

    def test_ref_added_returns_true(self):
        """User pinned a ref that wasn't pinned before."""
        locked = _LockedDep(resolved_ref=None)
        dep = _dep(reference="v1.0.0")
        self.assertTrue(detect_ref_change(dep, locked))

    def test_ref_removed_returns_true(self):
        """User removed the pin."""
        locked = _LockedDep(resolved_ref="main")
        dep = _dep(reference=None)
        self.assertTrue(detect_ref_change(dep, locked))

    def test_ref_changed_returns_true(self):
        """User bumped the pin."""
        locked = _LockedDep(resolved_ref="v1.0.0")
        dep = _dep(reference="v2.0.0")
        self.assertTrue(detect_ref_change(dep, locked))

    def test_hash_ref_changed_returns_true(self):
        """Hash-based pin change is detected."""
        locked = _LockedDep(resolved_ref="abc1234")
        dep = _dep(reference="def5678")
        self.assertTrue(detect_ref_change(dep, locked))

    def test_hash_ref_added_returns_true(self):
        locked = _LockedDep(resolved_ref=None)
        dep = _dep(reference="abc1234")
        self.assertTrue(detect_ref_change(dep, locked))


# ---------------------------------------------------------------------------
# detect_orphans
# ---------------------------------------------------------------------------


class TestDetectOrphans(unittest.TestCase):
    """Tests for detect_orphans()."""

    def test_partial_install_returns_empty(self):
        """Partial install must not clean up anything."""
        locked_dep = _LockedDep(repo_url="owner/orphan", deployed_files=["a.md"])
        lf = _LockFile(dependencies={"owner/orphan": locked_dep})
        result = detect_orphans(lf, {"owner/kept"}, only_packages=["owner/kept"])
        self.assertEqual(result, set())

    def test_no_lockfile_returns_empty(self):
        """First install has no previous lockfile."""
        result = detect_orphans(None, {"owner/pkg"}, only_packages=[])
        self.assertEqual(result, set())

    def test_all_packages_present_returns_empty(self):
        locked_dep = _LockedDep(repo_url="owner/pkg", deployed_files=["file.md"])
        lf = _LockFile(dependencies={"owner/pkg": locked_dep})
        result = detect_orphans(lf, {"owner/pkg"}, only_packages=[])
        self.assertEqual(result, set())

    def test_orphan_files_returned(self):
        """Files belonging to dropped packages are returned."""
        orphan = _LockedDep(repo_url="owner/gone", deployed_files=["x.md", "y.md"])
        kept = _LockedDep(repo_url="owner/kept", deployed_files=["z.md"])
        lf = _LockFile(dependencies={"owner/gone": orphan, "owner/kept": kept})
        result = detect_orphans(lf, {"owner/kept"}, only_packages=[])
        self.assertEqual(result, {"x.md", "y.md"})

    def test_multiple_orphans_combined(self):
        a = _LockedDep(repo_url="a/a", deployed_files=["a.md"])
        b = _LockedDep(repo_url="b/b", deployed_files=["b.md"])
        lf = _LockFile(dependencies={"a/a": a, "b/b": b})
        result = detect_orphans(lf, set(), only_packages=[])
        self.assertEqual(result, {"a.md", "b.md"})

    def test_empty_intended_deps(self):
        """Empty manifest means all current packages are orphans."""
        dep = _LockedDep(repo_url="owner/removed", deployed_files=["f.md"])
        lf = _LockFile(dependencies={"owner/removed": dep})
        result = detect_orphans(lf, set(), only_packages=[])
        self.assertEqual(result, {"f.md"})

    def test_none_only_packages_treated_as_empty(self):
        """only_packages=None is treated the same as [] -- full install."""
        locked_dep = _LockedDep(repo_url="owner/orphan", deployed_files=["a.md"])
        lf = _LockFile(dependencies={"owner/orphan": locked_dep})
        result = detect_orphans(lf, set(), only_packages=None)
        self.assertEqual(result, {"a.md"})


# ---------------------------------------------------------------------------
# detect_config_drift
# ---------------------------------------------------------------------------


class TestDetectConfigDrift(unittest.TestCase):
    """Tests for detect_config_drift()."""

    def test_empty_inputs_returns_empty(self):
        self.assertEqual(detect_config_drift({}, {}), set())

    def test_unchanged_config_returns_empty(self):
        cfg = {"server": {"cmd": "node"}}
        self.assertEqual(detect_config_drift({"s": cfg}, {"s": cfg}), set())

    def test_changed_config_detected(self):
        old = {"cmd": "node", "env": {}}
        new = {"cmd": "bun", "env": {}}
        result = detect_config_drift({"s": new}, {"s": old})
        self.assertIn("s", result)

    def test_new_entry_not_in_stored_ignored(self):
        """Brand-new entry has no baseline -- not drift."""
        result = detect_config_drift({"new": {"cmd": "x"}}, {})
        self.assertEqual(result, set())

    def test_only_drifted_entries_returned(self):
        current = {"a": {"v": 1}, "b": {"v": 2}}
        stored = {"a": {"v": 1}, "b": {"v": 99}}
        result = detect_config_drift(current, stored)
        self.assertEqual(result, {"b"})

    def test_multiple_drifted_entries(self):
        current = {"x": 1, "y": 2, "z": 3}
        stored = {"x": 9, "y": 2, "z": 8}
        result = detect_config_drift(current, stored)
        self.assertEqual(result, {"x", "z"})

    def test_stored_superset_not_counted(self):
        """Entries in stored but absent from current are not drift."""
        current = {"a": {"v": 1}}
        stored = {"a": {"v": 1}, "b": {"v": 2}}
        result = detect_config_drift(current, stored)
        self.assertEqual(result, set())


# ---------------------------------------------------------------------------
# build_download_ref
# ---------------------------------------------------------------------------


class TestBuildDownloadRef(unittest.TestCase):
    """Tests for build_download_ref()."""

    def _dep(self, **kwargs) -> DependencyReference:
        defaults = {"repo_url": "owner/repo"}
        defaults.update(kwargs)
        return DependencyReference(**defaults)

    # --- when locked info is not used ---

    def test_no_lockfile_returns_dep_ref(self):
        dep = self._dep(reference="v1.0.0")
        result = build_download_ref(dep, None, update_refs=False, ref_changed=False)
        self.assertIs(result, dep)

    def test_update_refs_returns_dep_ref(self):
        dep = self._dep(reference="v2.0.0")
        locked = _LockedDep(resolved_commit="abc123", resolved_ref="v1.0.0")
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=True, ref_changed=False)
        self.assertIs(result, dep)

    def test_ref_changed_returns_dep_ref(self):
        dep = self._dep(reference="v2.0.0")
        locked = _LockedDep(resolved_commit="abc123", resolved_ref="v1.0.0")
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=True)
        self.assertIs(result, dep)

    # --- reproducible: use locked commit SHA ---

    def test_locked_commit_used(self):
        """Reproducible install: locked commit SHA replaces manifest ref."""
        dep = self._dep(reference="main")
        locked = _LockedDep(repo_url="owner/repo", resolved_commit="deadbeef")
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        self.assertEqual(result.reference, "deadbeef")

    def test_cached_commit_not_used(self):
        """A 'cached' sentinel is not used as a reproducible commit."""
        dep = self._dep(reference="main")
        locked = _LockedDep(repo_url="owner/repo", resolved_commit="cached")
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        # No override should be applied for 'cached'
        self.assertEqual(result.reference, "main")

    # --- proxy deps: host + artifactory_prefix restored ---

    def test_proxy_dep_host_and_prefix_restored(self):
        """Proxy deps restore host and artifactory_prefix for correct auth."""
        dep = self._dep(reference="v1.0.0")
        locked = _LockedDep(
            repo_url="owner/repo",
            host="myartifactory.example.com",
            registry_prefix="artifactory/github",
            resolved_commit="abcdef",
        )
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        self.assertEqual(result.host, "myartifactory.example.com")
        self.assertEqual(result.artifactory_prefix, "artifactory/github")

    def test_proxy_dep_without_commit_uses_locked_ref(self):
        """Proxy dep without commit: fall back to locked_ref for reproducibility."""
        dep = self._dep(reference=None)
        locked = _LockedDep(
            repo_url="owner/repo",
            registry_prefix="artifactory/github",
            resolved_ref="v1.2.3",
            resolved_commit=None,
        )
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        self.assertEqual(result.reference, "v1.2.3")

    def test_proxy_dep_with_existing_ref_not_overridden(self):
        """If dep already has a ref and proxy has no commit, don't override."""
        dep = self._dep(reference="main")
        locked = _LockedDep(
            repo_url="owner/repo",
            registry_prefix="artifactory/github",
            resolved_ref="v1.2.3",
            resolved_commit=None,
        )
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        # dep.reference is "main" (truthy) so the fallback should not apply
        self.assertEqual(result.reference, "main")

    def test_non_proxy_host_difference_restores_host(self):
        """Non-proxy deps also restore host if it differs from manifest."""
        dep = DependencyReference(repo_url="owner/repo", host="github.com")
        locked = _LockedDep(
            repo_url="owner/repo",
            host="enterprise.github.example.com",
            registry_prefix=None,
            resolved_commit="sha123",
        )
        lf = _LockFile(dependencies={"owner/repo": locked})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        self.assertEqual(result.host, "enterprise.github.example.com")

    def test_dep_key_not_in_lockfile_returns_dep_ref(self):
        """If dep key isn't in lockfile, return dep as-is."""
        dep = self._dep(reference="main")
        lf = _LockFile(dependencies={})
        result = build_download_ref(dep, lf, update_refs=False, ref_changed=False)
        self.assertIs(result, dep)
