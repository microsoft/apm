"""Unit tests for ``apm_cli.install.heals.branch_ref_drift``.

These complement the integration-level coverage in
``tests/unit/install/test_branch_ref_drift.py`` by exercising the
heal class in isolation -- no install-context plumbing, no dispatcher.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from apm_cli.install.heals.base import HealContext, HealMessageLevel
from apm_cli.install.heals.branch_ref_drift import BranchRefDriftHeal
from apm_cli.models.apm_package import GitReferenceType


def _hctx(
    *,
    lockfile_match: bool = True,
    update_refs: bool = False,
    resolved_sha: str = "newsha1234",
    locked_sha: str | None = "oldsha5678",
    ref_type: GitReferenceType = GitReferenceType.BRANCH,
    has_lockfile: bool = True,
    apm_version: str = "0.13.0",
):
    rr = MagicMock()
    rr.ref_type = ref_type
    rr.resolved_commit = resolved_sha

    locked = None
    existing_lockfile = None
    if has_lockfile:
        existing_lockfile = MagicMock()
        existing_lockfile.apm_version = apm_version
        if locked_sha is not None:
            locked = MagicMock()
            locked.resolved_commit = locked_sha
            existing_lockfile.get_dependency.return_value = locked
        else:
            existing_lockfile.get_dependency.return_value = None

    dep_ref = MagicMock()
    dep_ref.get_unique_key.return_value = "github.com/owner/repo"

    return HealContext(
        dep_ref=dep_ref,
        package_key="github.com/owner/repo",
        resolved_ref=rr,
        existing_lockfile=existing_lockfile,
        lockfile_match=lockfile_match,
        lockfile_match_via_content_hash_only=False,
        update_refs=update_refs,
    )


class TestBranchRefDriftHealApplies:
    def test_applies_when_branch_remote_advanced(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(resolved_sha="newsha", locked_sha="oldsha")) is True

    def test_does_not_apply_when_remote_unchanged(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(resolved_sha="samesha", locked_sha="samesha")) is False

    def test_does_not_apply_for_tag_ref(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(ref_type=GitReferenceType.TAG)) is False

    def test_does_not_apply_when_lockfile_match_false(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(lockfile_match=False)) is False

    def test_does_not_apply_in_update_mode(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(update_refs=True)) is False

    def test_does_not_apply_without_resolved_ref(self):
        h = BranchRefDriftHeal()
        ctx = _hctx()
        ctx.resolved_ref = None
        assert h.applies(ctx) is False

    def test_does_not_apply_without_existing_lockfile(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(has_lockfile=False)) is False

    def test_does_not_apply_when_locked_sha_is_cached_sentinel(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(locked_sha="cached")) is False

    def test_does_not_apply_when_no_dep_in_lockfile(self):
        h = BranchRefDriftHeal()
        assert h.applies(_hctx(locked_sha=None)) is False


class TestBranchRefDriftHealExecute:
    def test_execute_flips_lockfile_match_and_emits_info(self):
        h = BranchRefDriftHeal()
        ctx = _hctx(resolved_sha="abcdef1234", locked_sha="9876543210")
        h.execute(ctx)
        assert ctx.lockfile_match is False
        assert ctx.ref_changed is True
        assert "github.com/owner/repo" in ctx.bypass_keys
        assert len(ctx.messages) == 1
        msg = ctx.messages[0]
        assert msg.level == HealMessageLevel.INFO
        assert "abcdef12" in msg.text
        assert "98765432" in msg.text

    def test_does_not_apply_when_resolved_commit_is_none(self):
        """Artifactory proxy / non-git source path: resolved_ref exists
        but resolved_commit is None. Without the guard, applies() would
        return True (None != "oldsha") and execute() would crash on
        slicing None[:8]. Regression test for Copilot review #3194552126.
        """
        h = BranchRefDriftHeal()
        ctx = _hctx(resolved_sha="oldsha")
        ctx.resolved_ref.resolved_commit = None
        assert h.applies(ctx) is False

    def test_does_not_apply_when_resolved_commit_is_cached_sentinel(self):
        h = BranchRefDriftHeal()
        ctx = _hctx()
        ctx.resolved_ref.resolved_commit = "cached"
        assert h.applies(ctx) is False

    def test_does_not_apply_when_resolved_commit_is_empty(self):
        h = BranchRefDriftHeal()
        ctx = _hctx()
        ctx.resolved_ref.resolved_commit = ""
        assert h.applies(ctx) is False


class TestBranchRefDriftHealMetadata:
    def test_chain_metadata(self):
        h = BranchRefDriftHeal()
        assert h.name == "branch_ref_drift"
        assert h.order == 10
        assert h.exclusive_group == "branch_drift"
