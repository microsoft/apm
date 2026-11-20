"""Branch-ref drift detection and self-heal against cached installs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.install.phases.integrate import _resolve_download_strategy
from apm_cli.models.apm_package import GitReferenceType

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-locked-copy-validity")]

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_resolved_ref(
    *, ref_type: GitReferenceType = GitReferenceType.BRANCH, sha: str = "newsha1234"
):
    rr = MagicMock()
    rr.ref_type = ref_type
    rr.resolved_commit = sha
    rr.ref_name = "main"
    rr.original_ref = "main"
    return rr


def _make_locked_dep(
    *,
    sha: str = "oldsha9999",
    content_hash: str | None = None,
    resolved_ref: str = "main",
    is_insecure: bool = False,
):
    locked = MagicMock()
    locked.resolved_commit = sha
    locked.content_hash = content_hash
    locked.registry_prefix = None
    locked.resolved_ref = resolved_ref
    locked.is_insecure = is_insecure
    return locked


def _make_dep_ref(*, key: str = "github.com/owner/repo", reference: str = "main"):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.reference = reference
    dep.is_local = False
    dep.is_virtual = False
    dep.local_path = None
    dep.repo_url = "https://github.com/owner/repo"
    dep.is_insecure = False
    return dep


def _make_ctx(
    *,
    update_refs: bool = False,
    locked_dep=None,
    apm_version: str | None = None,
    callback_downloaded: dict | None = None,
    pre_downloaded_keys: set | None = None,
    resolved_ref=None,
    registry_config=None,
):
    ctx = MagicMock()
    ctx.update_refs = update_refs
    ctx.callback_downloaded = callback_downloaded or {}
    ctx.pre_downloaded_keys = pre_downloaded_keys or set()
    ctx.registry_config = registry_config
    # Real set (not a MagicMock attr) so .add() actually populates it
    # and tests can assert membership. Without this, the supply-chain
    # bypass plumbing in integrate.py would silently regress without
    # any unit failure (caught only by E2E).
    ctx.expected_hash_change_deps = set()

    if locked_dep is None:
        ctx.existing_lockfile = None
    else:
        existing = MagicMock()
        existing.get_dependency.return_value = locked_dep
        existing.apm_version = apm_version
        ctx.existing_lockfile = existing

    ctx.diagnostics = MagicMock()
    ctx.logger = MagicMock()

    # downloader.resolve_git_reference returns the supplied resolved_ref
    ctx.downloader = MagicMock()
    if resolved_ref is None:
        ctx.downloader.resolve_git_reference.side_effect = Exception("no remote")
    else:
        ctx.downloader.resolve_git_reference.return_value = resolved_ref

    return ctx


# ----------------------------------------------------------------------
# Fix A -- branch-ref drift forces re-download
# ----------------------------------------------------------------------


class TestBranchRefDriftDetection:
    """Bug A: ``_resolve_download_strategy`` must NOT skip download when a
    branch ref's resolved SHA has advanced past the lockfile SHA, even if
    local content still matches the lockfile's recorded content_hash.
    """

    def test_branch_drift_forces_re_download(self, tmp_path: Path, monkeypatch):
        """Resolved branch SHA != lockfile SHA -> skip_download=False, ref_changed=True.

        This is the headline bug from the bug report. Without the guard,
        ``lockfile_match`` would fire via content-hash fallback (virtual-package
        case where install_path is not a git repo) and the install would skip
        download while ``CachedDependencySource`` writes the new SHA to the
        lockfile -- producing the 3-way inconsistency.
        """
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        # Lockfile recorded OLD SHA + OLD hash. Disk has OLD content matching
        # OLD hash. Remote has advanced to NEW SHA.
        locked = _make_locked_dep(sha="oldsha9999", content_hash="sha256:oldhash")
        resolved = _make_resolved_ref(sha="newsha1234")
        ctx = _make_ctx(locked_dep=locked, resolved_ref=resolved, apm_version="0.13.0")
        dep_ref = _make_dep_ref()

        # Force the content-hash fallback path (virtual-package shape):
        # GitRepo() raises, then verify_package_hash() returns True.
        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        resolved_out, skip_download, locked_out, ref_changed = _resolve_download_strategy(
            ctx, dep_ref, install_path
        )

        assert skip_download is False, (
            "Branch drift must force re-download; lockfile_match should be vetoed"
        )
        assert ref_changed is True, (
            "ref_changed must be True so build_download_ref does not pin to the OLD locked SHA"
        )
        assert resolved_out is resolved
        assert locked_out is locked
        # Regression trap: the supply-chain bypass MUST be populated.
        # If the ctx.expected_hash_change_deps.add(...) call in
        # integrate.py is ever removed, the supply-chain hard-block
        # at sources.py would call sys.exit(1) on the legitimate
        # fresh-download hash mismatch, killing the install.
        assert dep_ref.get_unique_key() in ctx.expected_hash_change_deps, (
            "expected_hash_change_deps must be populated so the "
            "supply-chain hard-block treats the legitimate drift-driven "
            "hash change as recovery evidence, not as an attack"
        )
        """Resolved branch SHA == lockfile SHA -> skip_download=True (no churn)."""
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        same_sha = "samesha5678"
        locked = _make_locked_dep(sha=same_sha, content_hash="sha256:h")
        resolved = _make_resolved_ref(sha=same_sha)
        ctx = _make_ctx(locked_dep=locked, resolved_ref=resolved, apm_version="0.13.0")
        dep_ref = _make_dep_ref()

        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        _, skip_download, _, ref_changed = _resolve_download_strategy(ctx, dep_ref, install_path)

        assert skip_download is True
        assert ref_changed is False

    def test_tag_ref_drift_does_not_trigger(self, tmp_path: Path, monkeypatch):
        """Tag refs are immutable by contract; a tag whose SHA somehow
        differs from lockfile must NOT trip the branch-drift guard (it
        would suggest a different bug -- tag mutation -- handled by the
        content-hash integrity check, not this guard)."""
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        locked = _make_locked_dep(sha="oldsha9999", content_hash="sha256:h", resolved_ref="v1.0.0")
        resolved = _make_resolved_ref(ref_type=GitReferenceType.TAG, sha="newsha1234")
        ctx = _make_ctx(locked_dep=locked, resolved_ref=resolved, apm_version="0.13.0")
        dep_ref = _make_dep_ref(reference="v1.0.0")

        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        _, skip_download, _, ref_changed = _resolve_download_strategy(ctx, dep_ref, install_path)

        # Tag is cacheable -> skip_download True via is_cacheable, not branch guard.
        # ref_changed must remain False (the branch-drift code path is not entered).
        assert skip_download is True
        assert ref_changed is False


# ----------------------------------------------------------------------
# Fix C -- self-heal for v<=0.12.2 lockfiles
# ----------------------------------------------------------------------


class TestSelfHealForUpgradingUsers:
    """Fix C: lockfiles generated by APM <= 0.12.2 with branch-ref deps
    that match via content-hash fallback (virtual package case) must
    trigger a one-time re-download even when no remote drift is detected.
    This recovers users whose previous installs left the lockfile in a
    silently-corrupted state."""

    def test_self_heal_triggers_for_buggy_lockfile_branch_ref(self, tmp_path: Path, monkeypatch):
        """Buggy-version lockfile + branch ref + content-hash fallback ->
        force re-download, even when remote SHA == lockfile SHA."""
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        same_sha = "samesha5678"
        locked = _make_locked_dep(sha=same_sha, content_hash="sha256:disk-matches")
        resolved = _make_resolved_ref(sha=same_sha)  # No remote drift
        ctx = _make_ctx(
            locked_dep=locked,
            resolved_ref=resolved,
            apm_version="0.12.2",  # buggy version
        )
        dep_ref = _make_dep_ref()

        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        _, skip_download, _, ref_changed = _resolve_download_strategy(ctx, dep_ref, install_path)

        assert skip_download is False, "Self-heal must force re-download"
        assert ref_changed is True
        # Visible user warning emitted
        assert ctx.diagnostics.warn.called
        warn_msg = ctx.diagnostics.warn.call_args.args[0]
        assert "branch-ref cache drift" in warn_msg
        assert "0.12.2" in warn_msg
        # Regression trap: the supply-chain bypass MUST be populated
        # by the self-heal path. In the exact corrupted
        # state (lockfile.content_hash records OLD bytes while disk
        # is also OLD), the forced re-download will produce NEW bytes
        # whose hash differs from the lockfile-recorded hash. Without
        # this set populated, sources.py supply-chain hard-block calls
        # sys.exit(1) BEFORE the lockfile is repaired -- killing the
        # very recovery path this branch is meant to provide.
        assert dep_ref.get_unique_key() in ctx.expected_hash_change_deps, (
            "Self-heal must populate expected_hash_change_deps so the "
            "supply-chain hard-block does not abort the recovery"
        )

    def test_self_heal_does_not_fire_for_current_version(self, tmp_path: Path, monkeypatch):
        """Lockfiles from non-buggy versions must not trigger self-heal."""
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        same_sha = "samesha5678"
        locked = _make_locked_dep(sha=same_sha, content_hash="sha256:h")
        resolved = _make_resolved_ref(sha=same_sha)
        ctx = _make_ctx(locked_dep=locked, resolved_ref=resolved, apm_version="0.13.0")
        dep_ref = _make_dep_ref()

        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        _, skip_download, _, _ = _resolve_download_strategy(ctx, dep_ref, install_path)

        assert skip_download is True
        assert not ctx.diagnostics.warn.called

    def test_self_heal_does_not_fire_for_tag_ref(self, tmp_path: Path, monkeypatch):
        """Self-heal targets branch refs only; tags are not affected by
        the original bug."""
        install_path = tmp_path / "apm_modules" / "owner" / "repo"
        install_path.mkdir(parents=True, exist_ok=True)
        (install_path / "apm.yml").write_text("name: test\n")

        same_sha = "samesha5678"
        locked = _make_locked_dep(sha=same_sha, content_hash="sha256:h", resolved_ref="v1.0.0")
        resolved = _make_resolved_ref(ref_type=GitReferenceType.TAG, sha=same_sha)
        ctx = _make_ctx(locked_dep=locked, resolved_ref=resolved, apm_version="0.12.2")
        dep_ref = _make_dep_ref(reference="v1.0.0")

        monkeypatch.setattr(
            "apm_cli.utils.content_hash.verify_package_hash",
            lambda path, h, **_kwargs: True,
        )

        _, skip_download, _, _ = _resolve_download_strategy(ctx, dep_ref, install_path)

        # is_cacheable=True for tag -> skip_download True; self-heal not triggered
        assert skip_download is True
        assert not ctx.diagnostics.warn.called


# ----------------------------------------------------------------------
# Fix B -- CachedDependencySource never lies about disk identity
# ----------------------------------------------------------------------
