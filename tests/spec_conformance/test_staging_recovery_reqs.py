"""Executable filesystem oracles for proposed staged-replacement recovery."""

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit.install import test_resolution_failure_recovery as recovery

pytestmark = pytest.mark.component


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize(
    "oracle",
    [
        recovery.test_cleanup_denial_preserves_original_failure_and_restores_manifest,
        recovery.test_failed_immediate_restore_keeps_both_errors_and_prior_bytes,
        recovery.test_denied_rollback_preserves_backups_and_restores_other_packages,
        recovery.test_commit_cleanup_failure_never_restores_a_partially_deleted_backup,
        recovery.test_manifest_restoration_denial_retains_byte_exact_snapshot,
        recovery.test_denied_relocation_restore_continues_remaining_rollback,
    ],
    ids=[
        "cleanup",
        "activation-and-restore",
        "independent-recovery",
        "committed",
        "manifest",
        "relocation",
    ],
)
def test_staged_recovery_preserves_errors_and_recoverable_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oracle: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    """Execute the byte-level transaction oracles, not static source assertions."""
    oracle(tmp_path, monkeypatch)


@pytest.mark.req("req-lk-023")
def test_active_candidate_is_not_abandoned_staging(tmp_path: Path) -> None:
    """Exercise the real cross-session file lock and cleanup boundary."""
    recovery.test_prepared_replacement_holds_activity_lock_until_rollback(tmp_path)
