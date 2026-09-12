"""Unit coverage for the canonical install transaction owner."""

from __future__ import annotations

import gc
import multiprocessing
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from filelock import FileLock

from apm_cli.cli import cli
from apm_cli.core.command_logger import _ValidationOutcome
from apm_cli.install.locking import acquire_lifecycle_lock, lifecycle_lock
from apm_cli.install.transaction import InstallTransaction, resolution_for_context
from apm_cli.models.results import InstallDisposition, InstallResult
from apm_cli.utils.path_security import PathTraversalError, safe_rmtree

pytestmark = pytest.mark.windows_compat


def _hold_workspace_transaction(
    manifest: str,
    modules: str,
    ready,
    release,
) -> None:
    transaction = InstallTransaction(
        manifest_path=Path(manifest),
        apm_modules_dir=Path(modules),
        validation=None,
        logger=None,
    )
    ready.set()
    if not release.wait(10):
        raise TimeoutError("test did not release workspace transaction")
    transaction.commit(InstallResult())


def _acquire_workspace_transaction(
    manifest: str,
    modules: str,
    attempting,
    acquired,
) -> None:
    attempting.set()
    transaction = InstallTransaction(
        manifest_path=Path(manifest),
        apm_modules_dir=Path(modules),
        validation=None,
        logger=None,
    )
    acquired.set()
    transaction.commit(InstallResult())


def _transaction(
    tmp_path: Path,
    validation: _ValidationOutcome | None = None,
) -> InstallTransaction:
    manifest = tmp_path / "apm.yml"
    manifest.write_bytes(b"name: fixture\r\nversion: 1.0.0\r\n")
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    return InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=validation,
        logger=MagicMock(),
    )


def test_total_validation_failure_rolls_back(tmp_path: Path) -> None:
    """An all-invalid batch is a failed validation, not a successful no-op."""
    transaction = _transaction(
        tmp_path,
        _ValidationOutcome(valid=[], invalid=[("bad", "not found")]),
    )
    transaction.manifest_path.write_text("changed\n", encoding="ascii")

    result = transaction.validation_result()

    assert result is not None
    assert result.disposition is InstallDisposition.VALIDATION_FAILED
    assert result.exit_code == 1
    assert result.committed is False
    assert transaction.manifest_path.read_bytes() == b"name: fixture\r\nversion: 1.0.0\r\n"


def test_mixed_validation_commits_as_partial_success(tmp_path: Path) -> None:
    """A batch with survivors commits and keeps the existing warning policy."""
    transaction = _transaction(
        tmp_path,
        _ValidationOutcome(
            valid=[("good", False)],
            invalid=[("bad", "not found")],
        ),
    )

    result = transaction.commit(InstallResult(installed_count=1))

    assert result.disposition is InstallDisposition.PARTIAL_SUCCESS
    assert result.exit_code == 0
    assert result.committed is True


@pytest.mark.parametrize(
    "disposition",
    [InstallDisposition.CANCELLED],
)
def test_non_mutating_dispositions_remain_uncommitted(
    tmp_path: Path,
    disposition: InstallDisposition,
) -> None:
    """Cancellation and dry-run results remain non-mutating and uncommitted."""
    transaction = _transaction(tmp_path)
    result = InstallResult(disposition=disposition)

    transaction.rollback()

    assert result.exit_code == 0
    assert result.committed is False


def test_dry_run_completion_preserves_auto_created_manifest(tmp_path: Path) -> None:
    """A successful dry-run keeps bootstrap configuration but rolls back modules."""
    manifest = tmp_path / "apm.yml"
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    transaction = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=None,
        logger=MagicMock(),
    )
    package = modules / "new-package"
    transaction.resolution.prepare_path(package)
    package.mkdir()
    manifest.write_text("name: created\n", encoding="ascii")
    result = InstallResult(disposition=InstallDisposition.DRY_RUN)

    completed = transaction.complete(result)
    transaction.__exit__(None, None, None)

    assert completed is result
    assert completed.committed is False
    assert manifest.read_text(encoding="ascii") == "name: created\n"
    assert not package.exists()


def test_success_commit_finalizes_resolution(tmp_path: Path) -> None:
    """A successful install finalizes the resolution journal."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "new-package"
    transaction.resolution.prepare_path(package)
    package.mkdir()

    result = transaction.commit(InstallResult(installed_count=1))

    assert result.disposition is InstallDisposition.SUCCESS
    assert result.committed is True
    assert package.is_dir()
    assert not (transaction.apm_modules_dir / ".apm-resolution-staging").exists()


def test_success_commit_removes_abandoned_resolution_staging_only(tmp_path: Path) -> None:
    """A successful install garbage-collects only owned staging directories."""
    transaction = _transaction(tmp_path)
    staging_parent = transaction.apm_modules_dir / ".apm-resolution-staging"
    abandoned = staging_parent / ("a" * 32)
    unrelated = staging_parent / "keep-me"
    (abandoned / "package").mkdir(parents=True)
    unrelated.mkdir()
    abandoned.with_suffix(".lock").write_text("", encoding="ascii")
    (abandoned / "package" / "marker").write_text("stale", encoding="ascii")
    (unrelated / "marker").write_text("keep", encoding="ascii")

    transaction.commit(InstallResult())

    assert not abandoned.exists()
    assert not abandoned.with_suffix(".lock").exists()
    assert (unrelated / "marker").read_text(encoding="ascii") == "keep"


def test_success_commit_preserves_lockless_legacy_staging(tmp_path: Path) -> None:
    """Lockless backups remain until the user confirms no legacy install is active."""
    transaction = _transaction(tmp_path)
    staging_parent = transaction.apm_modules_dir / ".apm-resolution-staging"
    legacy = staging_parent / ("b" * 32)
    (legacy / "package").mkdir(parents=True)
    (legacy / "package" / "marker").write_text("keep", encoding="ascii")

    transaction.commit(InstallResult())

    assert (legacy / "package" / "marker").read_text(encoding="ascii") == "keep"
    transaction._logger.warning.assert_called_once_with(
        "Could not safely remove 1 interrupted-install backup item. "
        "Stop other APM installs, then run again with --verbose "
        "to see paths you can delete manually."
    )
    assert (
        "legacy backup has no activity lock" in transaction._logger.verbose_detail.call_args[0][0]
    )


def test_cleanup_failure_cannot_roll_back_committed_package(tmp_path: Path) -> None:
    """Best-effort garbage collection cannot reopen a committed transaction."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"
    package.mkdir()
    (package / "marker").write_text("original", encoding="ascii")
    transaction.resolution.prepare_path(package)
    package.mkdir()
    (package / "marker").write_text("replacement", encoding="ascii")
    staging_parent = transaction.apm_modules_dir / ".apm-resolution-staging"
    abandoned = staging_parent / ("c" * 32)
    (abandoned / "stale").mkdir(parents=True)
    abandoned.with_suffix(".lock").write_text("", encoding="ascii")
    original_safe_rmtree = safe_rmtree

    def fail_abandoned_cleanup(path: Path, root: Path) -> None:
        if path == abandoned:
            raise OSError("permission denied")
        original_safe_rmtree(path, root)

    with patch(
        "apm_cli.install.resolution_staging.safe_rmtree",
        side_effect=fail_abandoned_cleanup,
    ):
        result = transaction.commit(InstallResult())
        transaction.rollback()

    assert result.committed is True
    assert (package / "marker").read_text(encoding="ascii") == "replacement"
    transaction._logger.warning.assert_called_once()
    assert "permission denied" in transaction._logger.verbose_detail.call_args[0][0]


def test_current_lock_cleanup_failure_is_reported_after_commit(tmp_path: Path) -> None:
    """A retained current-session lock gets the same actionable diagnostic."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"
    package.mkdir()
    transaction.resolution.prepare_path(package)
    package.mkdir()
    lock_path = transaction.resolution._staging_lock_path
    original_unlink = Path.unlink

    def fail_lock_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == lock_path:
            raise OSError("permission denied")
        original_unlink(path, missing_ok=missing_ok)

    with patch.object(Path, "unlink", autospec=True, side_effect=fail_lock_unlink):
        result = transaction.commit(InstallResult())

    assert result.committed is True
    transaction._logger.warning.assert_called_once_with(
        "Could not safely remove 1 interrupted-install backup item. "
        "Stop other APM installs, then run again with --verbose "
        "to see paths you can delete manually."
    )
    detail = transaction._logger.verbose_detail.call_args[0][0]
    assert str(lock_path) in detail
    assert "could not remove activity lock: permission denied" in detail

    rerun_logger = MagicMock()
    rerun_logger.verbose = True
    rerun = InstallTransaction(
        manifest_path=transaction.manifest_path,
        apm_modules_dir=transaction.apm_modules_dir,
        validation=None,
        logger=rerun_logger,
    )
    rerun.commit(InstallResult())

    rerun_logger.warning.assert_called_once_with(
        "Could not safely remove 1 interrupted-install backup item. "
        "Stop other APM installs, then delete the paths listed below manually."
    )
    rerun_detail = rerun_logger.verbose_detail.call_args[0][0]
    assert str(lock_path) in rerun_detail
    assert "orphaned activity lock remains" in rerun_detail


def test_rollback_releases_staging_lock_when_root_cleanup_fails(tmp_path: Path) -> None:
    """Rollback never leaves an activity lock held after restoration."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"
    package.mkdir()
    (package / "marker").write_text("original", encoding="ascii")
    transaction.resolution.prepare_path(package)
    package.mkdir()
    lock_path = transaction.resolution._staging_lock_path

    with (
        patch.object(
            transaction.resolution,
            "_remove_staging_root",
            side_effect=OSError("permission denied"),
        ),
        pytest.raises(OSError, match="permission denied"),
    ):
        transaction.rollback()

    probe = FileLock(str(lock_path))
    with probe.acquire(timeout=0):
        assert probe.is_locked


def test_success_commit_preserves_active_resolution_staging(tmp_path: Path) -> None:
    """Garbage collection does not remove another active install's backup."""
    active = _transaction(tmp_path)
    package = active.apm_modules_dir / "package"
    package.mkdir()
    (package / "marker").write_text("original", encoding="ascii")
    active.resolution.prepare_path(package)

    successful = InstallTransaction(
        manifest_path=active.manifest_path,
        apm_modules_dir=active.apm_modules_dir,
        validation=None,
        logger=MagicMock(),
    )
    successful.commit(InstallResult())
    active.rollback()

    assert (package / "marker").read_text(encoding="ascii") == "original"


def test_manifest_restore_is_byte_exact(tmp_path: Path) -> None:
    """Rollback restores the exact bytes captured before validation."""
    transaction = _transaction(tmp_path)
    original = transaction.manifest_path.read_bytes()
    transaction.manifest_path.write_bytes(b"name: changed\n")

    transaction.rollback()

    assert transaction.manifest_path.exists()
    assert transaction.manifest_path.read_bytes() == original


def test_rollback_removes_manifest_created_by_first_install(tmp_path: Path) -> None:
    """Rollback removes apm.yml when this attempt created it."""
    manifest = tmp_path / "apm.yml"
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    transaction = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=None,
        logger=MagicMock(),
    )
    manifest.write_text("name: created\n", encoding="ascii")

    transaction.rollback()

    assert not manifest.exists()


def test_rollback_reports_action_when_created_manifest_cannot_be_removed(
    tmp_path: Path,
) -> None:
    """A failed removal tells the user how to complete rollback."""
    manifest = tmp_path / "apm.yml"
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    logger = MagicMock()
    transaction = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=None,
        logger=logger,
    )
    manifest.write_text("name: created\n", encoding="ascii")

    with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        transaction.rollback()

    logger.warning.assert_called_once_with(
        "Failed to remove apm.yml created by this install. Delete apm.yml manually before retrying."
    )
    assert "permission denied" in logger.verbose_detail.call_args[0][0]


def test_rollback_removes_new_path_and_restores_existing_path(tmp_path: Path) -> None:
    """Rollback affects only paths prepared by this resolution attempt."""
    transaction = _transaction(tmp_path)
    new_path = transaction.apm_modules_dir / "new"
    existing_path = transaction.apm_modules_dir / "existing"
    existing_path.mkdir()
    (existing_path / "marker").write_text("original", encoding="ascii")

    transaction.resolution.prepare_path(new_path)
    new_path.mkdir()
    transaction.resolution.prepare_path(existing_path)
    existing_path.mkdir()
    (existing_path / "marker").write_text("replacement", encoding="ascii")
    transaction.rollback()

    assert not new_path.exists()
    assert (existing_path / "marker").read_text(encoding="ascii") == "original"


def test_concurrent_duplicate_prepare_is_idempotent(tmp_path: Path) -> None:
    """Concurrent duplicate prepares preserve one original snapshot."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"
    package.mkdir()
    (package / "marker").write_text("original", encoding="ascii")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(transaction.resolution.prepare_path, [package] * 32))
    package.mkdir(exist_ok=True)
    (package / "marker").write_text("replacement", encoding="ascii")
    transaction.rollback()

    assert (package / "marker").read_text(encoding="ascii") == "original"


def test_workspace_lock_serializes_concurrent_processes(tmp_path: Path) -> None:
    """Only one process may mutate a workspace transaction at a time."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: fixture\nversion: 1.0.0\n", encoding="ascii")
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    process_context = multiprocessing.get_context("spawn")
    holder_ready = process_context.Event()
    release_holder = process_context.Event()
    holder = process_context.Process(
        target=_hold_workspace_transaction,
        args=(str(manifest), str(modules), holder_ready, release_holder),
    )
    holder.start()
    assert holder_ready.wait(10)

    contenders = []
    contender_events = []
    for _ in range(4):
        attempting = process_context.Event()
        acquired = process_context.Event()
        contender = process_context.Process(
            target=_acquire_workspace_transaction,
            args=(str(manifest), str(modules), attempting, acquired),
        )
        contender.start()
        contenders.append(contender)
        contender_events.append((attempting, acquired))

    assert all(attempting.wait(10) for attempting, _ in contender_events)
    assert not any(acquired.wait(0.2) for _, acquired in contender_events)

    release_holder.set()
    assert all(acquired.wait(10) for _, acquired in contender_events)
    holder.join(10)
    for contender in contenders:
        contender.join(10)

    assert holder.exitcode == 0
    assert all(contender.exitcode == 0 for contender in contenders)


def test_workspace_lock_releases_after_interruption(tmp_path: Path) -> None:
    """Rollback releases the process lock after cancellation and errors."""
    transaction = _transaction(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        with transaction:
            raise KeyboardInterrupt()

    replacement = InstallTransaction(
        manifest_path=transaction.manifest_path,
        apm_modules_dir=transaction.apm_modules_dir,
        validation=None,
        logger=MagicMock(),
    )
    replacement.commit(InstallResult())


def test_workspace_lock_releases_when_initialization_fails(tmp_path: Path) -> None:
    """A constructor failure cannot strand the cross-process lock."""
    lock = lifecycle_lock()
    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: fixture\nversion: 1.0.0\n", encoding="ascii")
    modules = tmp_path / "apm_modules"
    modules.mkdir()

    with (
        patch(
            "apm_cli.install.transaction.ResolutionStagingSession",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom") as caught,
    ):
        InstallTransaction(
            manifest_path=manifest,
            apm_modules_dir=modules,
            validation=None,
            logger=MagicMock(),
        )

    assert caught.value.__traceback__ is not None
    assert lock.lock_counter == 0
    assert not lock.is_locked
    replacement = InstallTransaction(
        manifest_path=manifest,
        apm_modules_dir=modules,
        validation=None,
        logger=MagicMock(),
    )
    replacement.commit(InstallResult())


def test_workspace_lock_releases_when_commit_fails(tmp_path: Path) -> None:
    """A failed resolution commit rolls back before unlocking the workspace."""
    transaction = _transaction(tmp_path)
    transaction.resolution.commit = MagicMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        transaction.commit(InstallResult())

    replacement = InstallTransaction(
        manifest_path=transaction.manifest_path,
        apm_modules_dir=transaction.apm_modules_dir,
        validation=None,
        logger=MagicMock(),
    )
    replacement.commit(InstallResult())


def test_repeated_commit_releases_only_transaction_acquisition(tmp_path: Path) -> None:
    """Repeated completion cannot release the outer command lock."""
    outer_lock = acquire_lifecycle_lock()
    try:
        transaction = _transaction(tmp_path)
        result = InstallResult()

        transaction.commit(result)
        transaction.commit(result)

        assert outer_lock.is_locked
    finally:
        outer_lock.release()


def test_repeated_dry_run_releases_only_transaction_acquisition(tmp_path: Path) -> None:
    """Repeated dry-run completion cannot release the outer command lock."""
    outer_lock = acquire_lifecycle_lock()
    try:
        transaction = _transaction(tmp_path)
        result = InstallResult(disposition=InstallDisposition.DRY_RUN)

        transaction.complete(result)
        transaction.complete(result)

        assert outer_lock.is_locked
    finally:
        outer_lock.release()


@pytest.mark.parametrize("cyclic", [False, True])
def test_abandoned_transaction_does_not_strand_lifecycle_lock(tmp_path: Path, cyclic: bool) -> None:
    """Dropping the last transaction owner releases its FileLock acquisition."""
    lock = lifecycle_lock()
    transaction = _transaction(tmp_path)
    if cyclic:
        transaction._logger.transaction = transaction
    owner = weakref.ref(transaction)
    assert lock.is_locked
    del transaction
    gc.collect()

    assert owner() is None
    assert not lock.is_locked
    with FileLock(lock.lock_file).acquire(timeout=0) as probe:
        assert probe.is_locked
    replacement = InstallTransaction(
        manifest_path=tmp_path / "apm.yml",
        apm_modules_dir=tmp_path / "apm_modules",
        validation=None,
        logger=MagicMock(),
    )
    replacement.commit(InstallResult())


@pytest.mark.parametrize("completion", ["abandon", "commit", "rollback", "dry-run"])
def test_transaction_finalization_preserves_outer_lock(tmp_path: Path, completion: str) -> None:
    """Collection releases only an outstanding acquisition, never an outer owner."""
    outer_lock = acquire_lifecycle_lock()
    try:
        transaction = _transaction(tmp_path)
        assert outer_lock.lock_counter == 2
        if completion == "commit":
            transaction.commit(InstallResult())
        elif completion == "rollback":
            transaction.rollback()
        elif completion == "dry-run":
            transaction.complete(InstallResult(disposition=InstallDisposition.DRY_RUN))
        if completion != "abandon":
            assert outer_lock.lock_counter == 1
        del transaction
        gc.collect()

        assert outer_lock.lock_counter == 1
        assert outer_lock.is_locked
    finally:
        outer_lock.release()
    assert not outer_lock.is_locked


def test_phase_compatibility_journal_does_not_own_lifecycle_lock(tmp_path: Path) -> None:
    """Only the command/pipeline transaction owns lifecycle serialization."""
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    ctx = SimpleNamespace(
        transaction=None,
        source_root=tmp_path,
        apm_modules_dir=modules,
        logger=MagicMock(),
    )

    resolution_for_context(ctx)

    assert not lifecycle_lock().is_locked
    ctx.transaction.rollback()


def test_commit_only_after_cycle_validation(tmp_path: Path) -> None:
    """The resolution journal is finalized only after graph validation."""
    transaction = _transaction(tmp_path)
    cycle_validated = False
    original_commit = transaction.resolution.commit

    def checked_commit() -> None:
        assert cycle_validated
        original_commit()

    transaction.resolution.commit = checked_commit
    cycle_validated = True

    transaction.commit(InstallResult())


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), SystemExit(2), KeyboardInterrupt()],
)
def test_context_rolls_back_base_exceptions(tmp_path: Path, error: BaseException) -> None:
    """Exceptions, process exits, and interruptions all restore staged paths."""
    transaction = _transaction(tmp_path)
    package = transaction.apm_modules_dir / "package"

    with pytest.raises(type(error)):
        with transaction:
            transaction.resolution.prepare_path(package)
            package.mkdir()
            raise error

    assert not package.exists()


def test_fail_rolls_back_and_preserves_error(tmp_path: Path) -> None:
    """Failure returns a structured non-zero result after rollback."""
    transaction = _transaction(tmp_path)
    error = RuntimeError("failed")

    result = transaction.fail(error)

    assert result.disposition is InstallDisposition.FAILED
    assert result.exit_code == 1
    assert result.error is error
    assert result.committed is False


def test_no_cleanup_outside_apm_modules(tmp_path: Path) -> None:
    """The resolution journal rejects paths outside its owned root."""
    transaction = _transaction(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("keep", encoding="ascii")

    with pytest.raises(PathTraversalError):
        transaction.resolution.prepare_path(outside)
    transaction.rollback()

    assert (outside / "marker").read_text(encoding="ascii") == "keep"


def test_positional_url_total_failure_exits_one(tmp_path: Path, monkeypatch) -> None:
    """The Click boundary maps a structured total validation failure to 1."""
    (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    outcome = _ValidationOutcome(
        valid=[],
        invalid=[("https://example.invalid/missing", "not found")],
    )

    with patch(
        "apm_cli.commands.install._validate_and_add_packages_to_apm_yml",
        return_value=([], outcome),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "https://example.invalid/missing"],
        )

    assert result.exit_code == 1


def test_failed_first_install_removes_auto_created_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The transaction observes absence before the command creates apm.yml."""
    monkeypatch.chdir(tmp_path)
    outcome = _ValidationOutcome(
        valid=[],
        invalid=[("https://example.invalid/missing", "not found")],
    )

    with patch(
        "apm_cli.commands.install._validate_and_add_packages_to_apm_yml",
        return_value=([], outcome),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "https://example.invalid/missing"],
        )

    assert result.exit_code == 1
    assert not (tmp_path / "apm.yml").exists()
