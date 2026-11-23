"""A re-anchor whose lock is not saved rolls back and exits nonzero (#4889)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.commands.uninstall.cli import (
    _persist_lock_then_resync_native,
    _StillNeededRepair,
)
from apm_cli.commands.uninstall.engine import IntegrationCleanupOutcome
from apm_cli.commands.uninstall.local_reanchor import LocalReanchorPlan, snapshot_for_rollback

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I3", "P-INV-I1")]


def test_an_unsaved_reanchor_lock_restores_the_manifest_and_exits_nonzero(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    lock = tmp_path / "apm.lock.yaml"
    manifest.write_bytes(b"name: proj\r\ndependencies:\r\n  apm:\r\n    - ./vendor/e\r\n")
    lock.write_bytes(b"lockfile_version: '4'\r\n")
    plan = snapshot_for_rollback(
        LocalReanchorPlan(renames={"./vendor/e": "local:vendor/e"}), (manifest, lock)
    )
    original = (manifest.read_bytes(), lock.read_bytes())
    manifest.write_text("name: proj\n", encoding="utf-8")
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _persist_lock_then_resync_native(
            lock_changed=True,
            persist_allowed=False,
            lockfile=MagicMock(),
            lockfile_path=lock,
            logger=logger,
            integration_cleanup=IntegrationCleanupOutcome(
                counts={}, deployed_files={}, failed_paths=[], error_count=0
            ),
            deploy_root=tmp_path,
            modules_dir=tmp_path / "apm_modules",
            scope=None,
            native_before=None,
            still_needed_repairs=_StillNeededRepair({}, {}, {}, frozenset(), plan),
        )

    assert exc.value.code == 1
    assert (manifest.read_bytes(), lock.read_bytes()) == original
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "apm.yml and apm.lock.yaml were restored" in message
