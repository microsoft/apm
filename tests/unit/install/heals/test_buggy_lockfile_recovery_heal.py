"""Unit tests for ``apm_cli.install.heals.buggy_lockfile_recovery``."""

from __future__ import annotations

from unittest.mock import MagicMock

from apm_cli.install.heals.base import HealContext, HealMessageLevel
from apm_cli.install.heals.buggy_lockfile_recovery import (
    _BUGGY_BRANCH_REF_DRIFT_VERSIONS,
    BuggyLockfileRecoveryHeal,
    _is_buggy_lockfile_apm_version,
)
from apm_cli.models.apm_package import GitReferenceType


def _hctx(
    *,
    lockfile_match: bool = True,
    lockfile_match_via_content_hash_only: bool = True,
    update_refs: bool = False,
    ref_type: GitReferenceType = GitReferenceType.BRANCH,
    apm_version: str = "0.12.2",
    has_lockfile: bool = True,
):
    rr = MagicMock()
    rr.ref_type = ref_type
    rr.resolved_commit = "abc12345"

    existing_lockfile = None
    if has_lockfile:
        existing_lockfile = MagicMock()
        existing_lockfile.apm_version = apm_version

    dep_ref = MagicMock()
    dep_ref.get_unique_key.return_value = "github.com/owner/repo"

    return HealContext(
        dep_ref=dep_ref,
        package_key="github.com/owner/repo",
        resolved_ref=rr,
        existing_lockfile=existing_lockfile,
        lockfile_match=lockfile_match,
        lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
        update_refs=update_refs,
    )


class TestBuggyVersionDetection:
    def test_buggy_versions_in_set(self):
        for v in ("0.10.0", "0.11.0", "0.12.0", "0.12.1", "0.12.2"):
            assert v in _BUGGY_BRANCH_REF_DRIFT_VERSIONS

    def test_fixed_versions_not_in_set(self):
        for v in ("0.13.0", "0.13.1", "1.0.0"):
            assert v not in _BUGGY_BRANCH_REF_DRIFT_VERSIONS

    def test_none_lockfile_returns_false(self):
        assert _is_buggy_lockfile_apm_version(None) is False

    def test_missing_version_returns_false(self):
        lock = MagicMock()
        lock.apm_version = None
        assert _is_buggy_lockfile_apm_version(lock) is False

    def test_buggy_version_returns_true(self):
        lock = MagicMock()
        lock.apm_version = "0.12.2"
        assert _is_buggy_lockfile_apm_version(lock) is True

    def test_fixed_version_returns_false(self):
        lock = MagicMock()
        lock.apm_version = "0.13.0"
        assert _is_buggy_lockfile_apm_version(lock) is False


class TestBuggyLockfileRecoveryHealApplies:
    def test_applies_for_branch_buggy_version_content_hash_only(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx()) is True

    def test_does_not_apply_for_fixed_version(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx(apm_version="0.13.0")) is False

    def test_does_not_apply_for_tag_ref(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx(ref_type=GitReferenceType.TAG)) is False

    def test_does_not_apply_when_lockfile_match_via_git_head(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx(lockfile_match_via_content_hash_only=False)) is False

    def test_does_not_apply_in_update_mode(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx(update_refs=True)) is False

    def test_does_not_apply_without_lockfile(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.applies(_hctx(has_lockfile=False)) is False


class TestBuggyLockfileRecoveryHealExecute:
    def test_execute_emits_warn_and_populates_bypass(self):
        h = BuggyLockfileRecoveryHeal()
        ctx = _hctx()
        h.execute(ctx)
        assert ctx.lockfile_match is False
        assert ctx.ref_changed is True
        assert "github.com/owner/repo" in ctx.bypass_keys
        assert len(ctx.messages) == 1
        msg = ctx.messages[0]
        assert msg.level == HealMessageLevel.WARN
        assert "0.12.2" in msg.text
        assert "Recovering" in msg.text


class TestBuggyLockfileRecoveryHealMetadata:
    def test_chain_metadata(self):
        h = BuggyLockfileRecoveryHeal()
        assert h.name == "buggy_lockfile_recovery"
        assert h.order == 20
        assert h.exclusive_group == "branch_drift"
