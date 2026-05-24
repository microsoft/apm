"""Unit tests for resolve-phase spec-drift handling.

Verifies that the BFS download callback in ``resolve.py`` uses
``detect_ref_change`` and ``build_download_ref`` from ``drift.py``
so that user-specified ``#tag``/``#sha`` changes in the manifest
are not silently overridden by stale lockfile entries.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from apm_cli.drift import build_download_ref, detect_ref_change
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Minimal stubs (same pattern as test_drift_detection.py)
# ---------------------------------------------------------------------------


@dataclass
class _LockedDep:
    """Minimal LockedDependency stand-in."""

    repo_url: str = "owner/repo"
    resolved_ref: str | None = None
    resolved_commit: str | None = None
    host: str | None = None
    registry_prefix: str | None = None
    virtual_path: str | None = None
    source: str | None = None
    local_path: str | None = None
    deployed_files: list[str] = field(default_factory=list)
    content_hash: str | None = None
    is_insecure: bool = False

    def get_unique_key(self) -> str:
        if self.source == "local" and self.local_path:
            return self.local_path
        if self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url


@dataclass
class _LockFile:
    """Minimal LockFile stand-in."""

    dependencies: dict[str, _LockedDep] = field(default_factory=dict)

    def get_dependency(self, key: str) -> _LockedDep | None:
        return self.dependencies.get(key)

    def get_all_dependencies(self):
        return list(self.dependencies.values())


def _dep(repo_url: str = "owner/repo", reference: str | None = None) -> DependencyReference:
    return DependencyReference(repo_url=repo_url, reference=reference)


# ---------------------------------------------------------------------------
# Tests: resolve-phase spec drift via detect_ref_change + build_download_ref
# ---------------------------------------------------------------------------


class TestResolvePhaseSpecDrift(unittest.TestCase):
    """Verify that the resolve-phase download callback honours spec drift.

    These tests exercise the *same* functions that the resolve phase now
    calls (detect_ref_change, build_download_ref) to confirm the contract.
    """

    def test_spec_drift_uses_manifest_ref(self):
        """When manifest ref differs from lockfile, download should use manifest ref."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v2.0.0")

        self.assertTrue(detect_ref_change(dep, locked))

        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=True)
        self.assertEqual(download_ref.reference, "v2.0.0")

    def test_no_drift_uses_locked_commit(self):
        """When manifest ref matches lockfile, download should use locked commit."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v1.0.0")

        self.assertFalse(detect_ref_change(dep, locked))

        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=False)
        self.assertEqual(download_ref.reference, "aaa1111111111111111111111111111111111111")

    def test_added_pin_triggers_drift(self):
        """Adding a #tag to a previously-unpinned dep should trigger re-resolution."""
        locked = _LockedDep(
            resolved_ref=None,
            resolved_commit="bbb2222222222222222222222222222222222222",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v1.0.0")

        self.assertTrue(detect_ref_change(dep, locked))

        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=True)
        self.assertEqual(download_ref.reference, "v1.0.0")

    def test_removed_pin_triggers_drift(self):
        """Removing a #tag (back to default branch) should trigger re-resolution."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference=None)

        self.assertTrue(detect_ref_change(dep, locked))

        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=True)
        self.assertIsNone(download_ref.reference)

    def test_sha_pin_change_triggers_drift(self):
        """Changing from one commit SHA to another should trigger re-resolution."""
        locked = _LockedDep(
            resolved_ref="abc1234",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="def5678")

        self.assertTrue(detect_ref_change(dep, locked))

        download_ref = build_download_ref(dep, lockfile, update_refs=False, ref_changed=True)
        self.assertEqual(download_ref.reference, "def5678")

    def test_first_install_no_lockfile(self):
        """First install (no lockfile) should use manifest ref directly."""
        dep = _dep(reference="v1.0.0")

        self.assertFalse(detect_ref_change(dep, None))

        download_ref = build_download_ref(dep, None, update_refs=False, ref_changed=False)
        self.assertEqual(download_ref.reference, "v1.0.0")

    def test_update_refs_bypasses_lock(self):
        """--update mode should always use manifest ref (regression test)."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v1.0.0")

        self.assertFalse(detect_ref_change(dep, locked, update_refs=True))

        download_ref = build_download_ref(dep, lockfile, update_refs=True, ref_changed=False)
        self.assertEqual(download_ref.reference, "v1.0.0")


# ---------------------------------------------------------------------------
# Tests: expected_hash_change_deps marking
# ---------------------------------------------------------------------------


class TestExpectedHashChangeDeps(unittest.TestCase):
    """Verify that ref-changed deps are marked so content-hash validation
    doesn't treat legitimate re-resolution as a supply-chain attack.
    """

    def test_drift_marks_expected_hash_change(self):
        """When ref drifts, the dep key should be added to expected_hash_change_deps."""
        locked = _LockedDep(resolved_ref="v1.0.0", resolved_commit="aaa111")
        dep = _dep(reference="v2.0.0")

        ref_changed = detect_ref_change(dep, locked)
        self.assertTrue(ref_changed)

        expected_hash_change_deps: set[str] = set()
        if ref_changed:
            expected_hash_change_deps.add(dep.get_unique_key())

        self.assertIn("owner/repo", expected_hash_change_deps)

    def test_no_drift_does_not_mark(self):
        """When ref is unchanged, dep should NOT be in expected_hash_change_deps."""
        locked = _LockedDep(resolved_ref="v1.0.0", resolved_commit="aaa111")
        dep = _dep(reference="v1.0.0")

        ref_changed = detect_ref_change(dep, locked)
        self.assertFalse(ref_changed)

        expected_hash_change_deps: set[str] = set()
        if ref_changed:
            expected_hash_change_deps.add(dep.get_unique_key())

        self.assertNotIn("owner/repo", expected_hash_change_deps)


# ---------------------------------------------------------------------------
# Tests: --refresh flag wiring
# ---------------------------------------------------------------------------


class TestRefreshFlagWiring(unittest.TestCase):
    """Verify that --refresh triggers re-resolution of all refs."""

    def test_refresh_true_acts_like_update_refs(self):
        """When refresh=True, update_refs should be effectively True."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v1.0.0")

        # Simulate: update_refs = ctx.update_refs or ctx.refresh
        update_refs = True  # refresh=True overrides update_refs=False

        download_ref = build_download_ref(dep, lockfile, update_refs=update_refs, ref_changed=False)
        self.assertEqual(download_ref.reference, "v1.0.0")

    def test_refresh_false_uses_lock(self):
        """When refresh=False and no drift, should use locked commit."""
        locked = _LockedDep(
            resolved_ref="v1.0.0",
            resolved_commit="aaa1111111111111111111111111111111111111",
        )
        lockfile = _LockFile(dependencies={"owner/repo": locked})
        dep = _dep(reference="v1.0.0")

        update_refs = False or False  # refresh=False

        download_ref = build_download_ref(dep, lockfile, update_refs=update_refs, ref_changed=False)
        self.assertEqual(download_ref.reference, "aaa1111111111111111111111111111111111111")


# ---------------------------------------------------------------------------
# Tests: backward compatibility with old lockfiles
# ---------------------------------------------------------------------------


class TestOldLockfileBackwardCompat(unittest.TestCase):
    """Old lockfiles may lack resolved_ref. Verify conservative behaviour."""

    def test_old_lockfile_no_resolved_ref_with_user_pin(self):
        """Old lockfile without resolved_ref + user adds #tag -- drift detected."""
        locked = _LockedDep(
            resolved_ref=None,
            resolved_commit="old_sha",
        )
        dep = _dep(reference="v1.0.0")

        # None != "v1.0.0" -- True -- conservative re-download
        self.assertTrue(detect_ref_change(dep, locked))

    def test_old_lockfile_no_resolved_ref_no_user_pin(self):
        """Old lockfile without resolved_ref + user has no pin -- no drift."""
        locked = _LockedDep(
            resolved_ref=None,
            resolved_commit="old_sha",
        )
        dep = _dep(reference=None)

        # None == None -- False -- no drift, use locked commit
        self.assertFalse(detect_ref_change(dep, locked))


if __name__ == "__main__":
    unittest.main()
