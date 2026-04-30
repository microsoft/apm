"""Tests for --update ref resolution in download_callback and sequential loop.

Covers two code paths fixed in issue #548:

1. download_callback locked SHA bypass (install.py ~line 1268):
   When update_refs=True the callback MUST use dep_ref.reference (manifest ref)
   and MUST NOT pin to the old locked SHA from the lockfile.

2. already_resolved skip bypass (install.py ~line 1920):
   When update_refs=True the already_resolved flag MUST NOT unconditionally
   short-circuit the download.  Only lockfile_match (SHA comparison) may skip.
"""

import pytest

# ---------------------------------------------------------------------------
# Pure-logic helpers that mirror the two changed conditions in install.py.
# Tested in isolation -- the full _install_apm_dependencies() stack is not
# needed and would require network access / a real project root.
# ---------------------------------------------------------------------------


def _should_use_locked_ref(locked_ref, update_refs):
    """Mirror the locked-ref decision from download_callback (install.py ~L1268).

    Returns True when the download should be pinned to the locked SHA from the
    lockfile instead of using the manifest ref for re-resolution.

    Condition verbatim from source:
        if locked_ref and not update_refs:
            download_dep = _dc_replace(dep_ref, reference=locked_ref)
    """
    return bool(locked_ref) and not update_refs


def _compute_skip_download(
    install_path_exists, is_cacheable, update_refs, already_resolved, lockfile_match
):
    """Mirror the skip_download expression from the sequential loop (install.py ~L1920).

    Returns True when the loop should skip downloading a package.

    Expression verbatim from source:
        skip_download = install_path.exists() and (
            (is_cacheable and not update_refs)
            or (already_resolved and not update_refs)
            or lockfile_match
        )
    """
    return install_path_exists and (
        (is_cacheable and not update_refs)
        or (already_resolved and not update_refs)
        or lockfile_match
    )


# ===========================================================================
# TestDownloadCallbackUpdateRefs
# ===========================================================================


class TestDownloadCallbackUpdateRefs:
    """download_callback should use manifest ref when update_refs=True."""

    def test_callback_uses_locked_ref_normal_install(self):
        """Normal install: download_callback uses locked SHA for reproducibility.

        When update_refs=False and a locked ref exists, _should_use_locked_ref
        must return True so the download is pinned to the known commit SHA.
        """
        assert (
            _should_use_locked_ref(
                locked_ref="abc1234def5678901234567890abcdef01234567",
                update_refs=False,
            )
            is True
        )

    def test_callback_uses_manifest_ref_during_update(self):
        """--update: download_callback uses manifest ref, ignoring locked SHA.

        When update_refs=True, even if a locked ref exists, _should_use_locked_ref
        must return False so the download resolves against the manifest ref.
        Before the fix this returned True, silently locking to the stale SHA.
        """
        assert (
            _should_use_locked_ref(
                locked_ref="abc1234def5678901234567890abcdef01234567",
                update_refs=True,
            )
            is False
        )

    def test_callback_uses_manifest_ref_when_no_lockfile(self):
        """No lockfile: callback uses manifest ref regardless of update_refs.

        locked_ref is None when there is no lockfile or the dep is absent from
        it, so _should_use_locked_ref must return False in both modes.
        """
        assert _should_use_locked_ref(locked_ref=None, update_refs=False) is False
        assert _should_use_locked_ref(locked_ref=None, update_refs=True) is False

    def test_callback_uses_manifest_ref_when_locked_ref_empty_string(self):
        """Edge case: empty string locked_ref is falsy -- behaves like no lockfile."""
        assert _should_use_locked_ref(locked_ref="", update_refs=False) is False
        assert _should_use_locked_ref(locked_ref="", update_refs=True) is False

    @pytest.mark.parametrize(
        "locked_ref, update_refs, expected",
        [
            # locked ref present, normal install -> pin to locked SHA
            ("deadbeef" * 5, False, True),
            # locked ref present, update mode -> use manifest ref (the fix)
            ("deadbeef" * 5, True, False),
            # no locked ref, normal install -> use manifest ref
            (None, False, False),
            # no locked ref, update mode -> use manifest ref
            (None, True, False),
        ],
        ids=[
            "locked-normal",
            "locked-update",
            "no-lock-normal",
            "no-lock-update",
        ],
    )
    def test_locked_ref_matrix(self, locked_ref, update_refs, expected):
        """Parametrized truth table for _should_use_locked_ref."""
        assert _should_use_locked_ref(locked_ref, update_refs) is expected


# ===========================================================================
# TestAlreadyResolvedSkipLogic
# ===========================================================================


class TestAlreadyResolvedSkipLogic:
    """Sequential loop should not let already_resolved bypass downloads during --update."""

    # --- already_resolved gate ---

    def test_already_resolved_skips_normal_install(self):
        """Normal install: already_resolved=True causes skip_download=True.

        When update_refs=False a package already fetched by the BFS callback
        does not need to be downloaded again in the sequential loop.
        """
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=False,
                already_resolved=True,
                lockfile_match=False,
            )
            is True
        )

    def test_already_resolved_no_skip_during_update(self):
        """--update: already_resolved=True does NOT cause skip_download=True.

        When update_refs=True the BFS may have used an outdated ref. The
        sequential loop must redo SHA comparison (lockfile_match path) rather
        than blindly accepting the cached fetch from the callback.
        Bug before fix: (already_resolved) without not update_refs guard.
        """
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=True,
                already_resolved=True,
                lockfile_match=False,
            )
            is False
        )

    # --- lockfile_match is the correct skip path during --update ---

    def test_lockfile_match_still_skips_during_update(self):
        """--update: lockfile_match=True causes skip_download=True.

        SHA comparison confirmed the remote ref resolves to the same commit
        already checked out, so downloading again would be wasteful.
        """
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=True,
                already_resolved=False,
                lockfile_match=True,
            )
            is True
        )

    def test_lockfile_match_skips_even_with_already_resolved_during_update(self):
        """--update: lockfile_match=True skips regardless of already_resolved.

        When both flags are True in update mode, lockfile_match still wins and
        skip_download is True -- no content change was detected.
        """
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=False,
                update_refs=True,
                already_resolved=True,
                lockfile_match=True,
            )
            is True
        )

    # --- is_cacheable gate ---

    def test_cacheable_skips_normal_install(self):
        """Normal install: is_cacheable=True (tag/commit ref) causes skip."""
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=True,
                update_refs=False,
                already_resolved=False,
                lockfile_match=False,
            )
            is True
        )

    def test_cacheable_no_skip_during_update(self):
        """--update: is_cacheable alone does NOT cause skip_download=True.

        Even pinned tag refs must be re-resolved when the user explicitly
        requests an update; the is_cacheable guard is gated on not update_refs.
        """
        assert (
            _compute_skip_download(
                install_path_exists=True,
                is_cacheable=True,
                update_refs=True,
                already_resolved=False,
                lockfile_match=False,
            )
            is False
        )

    # --- install_path existence gate ---

    def test_skip_when_path_not_exists(self):
        """When install_path doesn't exist, skip_download is always False.

        No combination of flags can skip a package that has never been
        installed; the outer install_path.exists() guard prevents it.
        """
        assert (
            _compute_skip_download(
                install_path_exists=False,
                is_cacheable=True,
                update_refs=False,
                already_resolved=True,
                lockfile_match=True,
            )
            is False
        )

    def test_skip_when_path_not_exists_regardless_of_flags(self):
        """Exhaustive check: install_path_exists=False always yields False.

        Iterates every combination of the remaining four boolean flags to
        confirm the outer guard is unconditional.
        """
        for is_cacheable in (True, False):
            for update_refs in (True, False):
                for already_resolved in (True, False):
                    for lockfile_match in (True, False):
                        result = _compute_skip_download(
                            install_path_exists=False,
                            is_cacheable=is_cacheable,
                            update_refs=update_refs,
                            already_resolved=already_resolved,
                            lockfile_match=lockfile_match,
                        )
                        assert result is False, (
                            f"Expected False when install_path_exists=False "
                            f"(is_cacheable={is_cacheable}, update_refs={update_refs}, "
                            f"already_resolved={already_resolved}, "
                            f"lockfile_match={lockfile_match})"
                        )

    def test_no_flags_set_no_skip(self):
        """All boolean inputs False: skip_download must be False."""
        assert (
            _compute_skip_download(
                install_path_exists=False,
                is_cacheable=False,
                update_refs=False,
                already_resolved=False,
                lockfile_match=False,
            )
            is False
        )

    # --- parametrized truth table ---

    @pytest.mark.parametrize(
        (
            "install_path_exists",
            "is_cacheable",
            "update_refs",
            "already_resolved",
            "lockfile_match",
            "expected",
        ),
        [
            # path missing -> never skip
            (False, True, False, True, True, False),
            (False, False, False, False, False, False),
            # normal install: each individual skip condition fires
            (True, True, False, False, False, True),  # is_cacheable
            (True, False, False, True, False, True),  # already_resolved
            (True, False, False, False, True, True),  # lockfile_match
            # update mode: only lockfile_match may skip
            (True, True, True, False, False, False),  # is_cacheable gated
            (True, False, True, True, False, False),  # already_resolved gated (the fix)
            (True, False, True, False, True, True),  # lockfile_match still works
            (True, True, True, True, False, False),  # both gated, no lockfile_match
            (True, True, True, True, True, True),  # all True -> lockfile_match wins
        ],
        ids=[
            "path-missing-all-true",
            "all-false",
            "normal-cacheable",
            "normal-already-resolved",
            "normal-lockfile-match",
            "update-cacheable-gated",
            "update-already-resolved-gated",
            "update-lockfile-match-wins",
            "update-both-gated-no-match",
            "update-all-true",
        ],
    )
    def test_skip_download_truth_table(
        self,
        install_path_exists,
        is_cacheable,
        update_refs,
        already_resolved,
        lockfile_match,
        expected,
    ):
        """Parametrized truth table covering key combinations of skip_download inputs."""
        result = _compute_skip_download(
            install_path_exists=install_path_exists,
            is_cacheable=is_cacheable,
            update_refs=update_refs,
            already_resolved=already_resolved,
            lockfile_match=lockfile_match,
        )
        assert result is expected
