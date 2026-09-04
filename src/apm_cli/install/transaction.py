"""Canonical completion and rollback owner for one install attempt."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from apm_cli.install.locking import acquire_lifecycle_lock
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger, _ValidationOutcome


def _restore_manifest_from_snapshot(manifest_path: Path, snapshot: bytes) -> None:
    """Atomically replace *manifest_path* with byte-exact *snapshot*."""
    fd, temporary_name = tempfile.mkstemp(
        prefix="apm-restore-",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot)
        os.replace(temporary_name, manifest_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary_name)
        raise


def _maybe_rollback_manifest(
    manifest_path: Path,
    snapshot: bytes | None,
    logger: InstallLogger,
    manifest_existed: bool | None = None,
) -> None:
    """Best-effort restore or removal of the attempt's manifest."""
    if manifest_existed is None:
        manifest_existed = snapshot is not None or manifest_path.exists()
    if not manifest_existed:
        if not manifest_path.exists():
            return
        try:
            manifest_path.unlink()
            if logger is not None:
                logger.progress("Removed apm.yml created by the failed install.")
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    "Failed to remove apm.yml created by this install. "
                    "Delete apm.yml manually before retrying."
                )
                logger.verbose_detail(f"Manifest rollback error: {exc}")
        return
    try:
        if snapshot is None:
            return
        if manifest_path.exists() and manifest_path.read_bytes() == snapshot:
            return
        _restore_manifest_from_snapshot(manifest_path, snapshot)
        if logger is not None:
            logger.progress("apm.yml restored to its previous state.")
    except Exception as exc:
        if logger is not None:
            logger.warning("Failed to restore apm.yml. Inspect apm.yml before retrying.")
            logger.verbose_detail(f"Manifest rollback error: {exc}")


class InstallTransaction:
    """Own install completion meaning and rollback-scoped filesystem state.

    The resolution journal is intentionally limited to paths prepared below
    ``apm_modules``. Native target integrations are outside this transaction.
    """

    def __init__(
        self,
        *,
        manifest_path: Path,
        apm_modules_dir: Path,
        validation: _ValidationOutcome | None,
        logger: InstallLogger,
        acquire_lock: bool = True,
    ) -> None:
        """Capture the manifest and create one resolution staging session."""
        self.manifest_path = manifest_path
        self.apm_modules_dir = apm_modules_dir
        self._validation = validation
        self._logger = logger
        self._workspace_lock = acquire_lifecycle_lock() if acquire_lock else None
        self._workspace_lock_held = self._workspace_lock is not None
        try:
            self._manifest_existed = manifest_path.exists()
            self._manifest_snapshot = manifest_path.read_bytes() if self._manifest_existed else None
            self._resolution = ResolutionStagingSession(apm_modules_dir)
        except BaseException:
            self._release_workspace_lock()
            raise
        self._lock = threading.RLock()
        self.committed = False
        self._completed = False
        self._rolled_back = False

    @property
    def resolution(self) -> ResolutionStagingSession:
        """Return the single resolution journal owned by this attempt."""
        return self._resolution

    def record_validation(self, validation: _ValidationOutcome) -> None:
        """Attach the validation outcome produced after transaction creation."""
        self._validation = validation

    def validation_result(self) -> InstallResult | None:
        """Return the terminal result for an all-invalid positional batch."""
        if self._validation is None or not self._validation.all_failed:
            return None
        self.rollback()
        return InstallResult(
            disposition=InstallDisposition.VALIDATION_FAILED,
            exit_code=1,
        )

    def commit(self, result: InstallResult) -> InstallResult:
        """Finalize staged resolution paths and mark *result* committed."""
        with self._lock:
            if self._rolled_back:
                raise RuntimeError("Cannot commit an install transaction after rollback")
            try:
                if not self.committed:
                    cleanup_issues = self._resolution.commit() or []
                    self.committed = True
                    self._completed = True
                    cleanup_issues.extend(self._resolution.remove_abandoned_roots())
                    self._report_resolution_cleanup_issues(cleanup_issues)
                if (
                    self._validation is not None
                    and self._validation.has_failures
                    and result.disposition is InstallDisposition.SUCCESS
                ):
                    result.disposition = InstallDisposition.PARTIAL_SUCCESS
                result.committed = True
                return result
            except BaseException:
                self.rollback()
                raise
            finally:
                if self.committed:
                    self._release_workspace_lock()

    def _report_resolution_cleanup_issues(self, issues: list[tuple[Path, str]]) -> None:
        """Report cleanup paths that require safe manual recovery."""
        if not issues or self._logger is None:
            return
        item_label = "item" if len(issues) == 1 else "items"
        if self._logger.verbose is True:
            recovery_action = (
                "Stop other APM installs, then delete the paths listed below manually."
            )
        else:
            recovery_action = (
                "Stop other APM installs, then run again with --verbose "
                "to see paths you can delete manually."
            )
        self._logger.warning(
            f"Could not safely remove {len(issues)} interrupted-install backup "
            f"{item_label}. {recovery_action}"
        )
        for path, reason in issues:
            self._logger.verbose_detail(f"Resolution backup kept at {path}: {reason}")

    def complete(self, result: InstallResult) -> InstallResult:
        """Finalize one result according to its canonical disposition."""
        if result.disposition is InstallDisposition.DRY_RUN:
            with self._lock:
                if self._rolled_back:
                    raise RuntimeError("Cannot complete an install transaction after rollback")
                try:
                    cleanup_issues = self._resolution.rollback() or []
                    self._report_resolution_cleanup_issues(cleanup_issues)
                    self._completed = True
                finally:
                    self._release_workspace_lock()
            return result
        if result.disposition in {
            InstallDisposition.CANCELLED,
            InstallDisposition.FAILED,
            InstallDisposition.VALIDATION_FAILED,
        }:
            self.rollback()
            return result
        return self.commit(result)

    def rollback(self) -> None:
        """Restore the manifest and only resolution paths prepared here."""
        with self._lock:
            if self.committed or self._rolled_back:
                return
            try:
                cleanup_issues = self._resolution.rollback() or []
                self._report_resolution_cleanup_issues(cleanup_issues)
                self._restore_manifest()
                self._rolled_back = True
                self._completed = True
            finally:
                self._release_workspace_lock()

    def fail(self, error: BaseException) -> InstallResult:
        """Rollback and return a structured failed install result."""
        self.rollback()
        return InstallResult(
            disposition=InstallDisposition.FAILED,
            exit_code=1,
            error=error,
        )

    def __enter__(self) -> InstallTransaction:
        """Enter this install attempt."""
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        """Rollback every uncommitted exit and preserve exception semantics."""
        if exc is not None or not self._completed:
            self.rollback()
        return False

    def _restore_manifest(self) -> None:
        """Atomically restore the byte-exact manifest snapshot when present."""
        _maybe_rollback_manifest(
            self.manifest_path,
            self._manifest_snapshot,
            self._logger,
            self._manifest_existed,
        )

    def _release_workspace_lock(self) -> None:
        """Release this transaction's nested lifecycle acquisition once."""
        lock = self._workspace_lock
        if self._workspace_lock_held and lock is not None:
            self._workspace_lock_held = False
            lock.release()


def resolution_for_context(ctx: Any) -> ResolutionStagingSession:
    """Return the context transaction's journal, creating a legacy adapter."""
    if ctx.transaction is None:
        ctx.transaction = InstallTransaction(
            manifest_path=ctx.source_root / "apm.yml",
            apm_modules_dir=ctx.apm_modules_dir,
            validation=None,
            logger=ctx.logger,
            acquire_lock=False,
        )
    return cast(ResolutionStagingSession, ctx.transaction.resolution)
