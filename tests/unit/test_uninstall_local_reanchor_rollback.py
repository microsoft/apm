"""A re-anchor whose lock is not saved rolls back and exits nonzero (#4889)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.commands.uninstall.cli import (
    _persist_lock_then_resync_native,
    _refuse_reanchor_refresh,
    _rename_reanchored_receipts,
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


def _planned(tmp_path: Path):
    manifest = tmp_path / "apm.yml"
    lock = tmp_path / "apm.lock.yaml"
    manifest.write_text("name: proj\ndependencies:\n  apm:\n    - ./vendor/e\n", encoding="utf-8")
    lock.write_text("lockfile_version: '4'\n", encoding="utf-8")
    plan = snapshot_for_rollback(
        LocalReanchorPlan(renames={"./vendor/e": "local:vendor/e"}), (manifest, lock)
    )
    manifest.write_text("name: proj\n", encoding="utf-8")
    return manifest, lock, _StillNeededRepair({}, {}, {}, frozenset(), plan)


def test_a_survivor_refresh_during_a_reanchor_rolls_back(tmp_path: Path) -> None:
    manifest, _lock, repair = _planned(tmp_path)
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _refuse_reanchor_refresh(repair, {"local:vendor/f"}, logger)

    assert exc.value.code == 1
    assert "./vendor/e" in manifest.read_text(encoding="utf-8")
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "must be redeployed" in message and "were restored" in message


def test_a_failed_rollback_names_what_it_kept_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _lock, repair = _planned(tmp_path)
    real = __import__("apm_cli.integration.write_seam", fromlist=["x"]).atomic_write_text

    def refuse_manifest(path, *args, **kwargs):
        if Path(path).name == "apm.yml":
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr("apm_cli.integration.write_seam.atomic_write_text", refuse_manifest)
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _refuse_reanchor_refresh(repair, {"local:vendor/f"}, logger)

    assert exc.value.code == 1
    backup = tmp_path / "apm.yml.apm-reanchor-backup"
    assert "./vendor/e" in backup.read_text(encoding="utf-8")
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "could not be restored" in message and str(backup) in message, message
    assert "were restored" not in message
    assert manifest.read_text(encoding="utf-8") == "name: proj\n"


def test_a_failed_postcommit_binding_rolls_the_reanchor_back(tmp_path: Path) -> None:
    manifest, lock, repair = _planned(tmp_path)
    lockfile = MagicMock()
    session = MagicMock()
    session.bind_committed_claims.side_effect = RuntimeError("receipt store unavailable")
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _persist_lock_then_resync_native(
            lock_changed=True,
            persist_allowed=True,
            lockfile=lockfile,
            lockfile_path=lock,
            logger=logger,
            integration_cleanup=IntegrationCleanupOutcome(
                counts={},
                deployed_files={},
                failed_paths=[],
                error_count=0,
                materialization_session=session,
            ),
            deploy_root=tmp_path,
            modules_dir=tmp_path / "apm_modules",
            scope=None,
            native_before=None,
            still_needed_repairs=repair,
        )

    assert exc.value.code == 1
    assert "./vendor/e" in manifest.read_text(encoding="utf-8")
    assert lock.read_text(encoding="utf-8") == "lockfile_version: '4'\n"
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "were restored" in message, message


def test_any_failed_receipt_rename_rolls_the_reanchor_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    manifest, lock, repair = _planned(tmp_path)
    row = LockedDependency(
        repo_url="_local/e",
        source="local",
        local_path="../e",
        declaring_parent="./vendor/d",
        anchored_local_path="vendor/e",
    )
    LockFile(dependencies={row.get_unique_key(): row}).write(lock)
    repair = repair._replace(
        reanchor=repair.reanchor.__class__(
            renames={"./vendor/e": row.get_unique_key()}, snapshot=repair.reanchor.snapshot
        )
    )

    def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected receipt failure")

    monkeypatch.setattr("apm_cli.core.deployment_reanchor.rename_receipt_owners", broken)
    logger = MagicMock()

    with pytest.raises(SystemExit) as exc:
        _rename_reanchored_receipts(repair, lock, tmp_path, None, logger)

    assert exc.value.code == 1
    assert "./vendor/e" in manifest.read_text(encoding="utf-8")
    assert lock.read_text(encoding="utf-8") == "lockfile_version: '4'\n"
    message = " ".join(str(call.args[0]) for call in logger.error.call_args_list)
    assert "unexpected receipt failure" in message and "were restored" in message, message
