"""Canonical completion and rollback owner for one install attempt."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger, _ValidationOutcome


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
    ) -> None:
        """Capture the manifest and create one resolution staging session."""
        self.manifest_path = manifest_path
        self.apm_modules_dir = apm_modules_dir
        self._validation = validation
        self._logger = logger
        self._manifest_snapshot = manifest_path.read_bytes() if manifest_path.exists() else None
        self._resolution = ResolutionStagingSession(apm_modules_dir)
        self._lock = threading.RLock()
        self.committed = False
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
            if not self.committed:
                self._resolution.commit()
                self.committed = True
            if (
                self._validation is not None
                and self._validation.has_failures
                and result.disposition is InstallDisposition.SUCCESS
            ):
                result.disposition = InstallDisposition.PARTIAL_SUCCESS
            result.committed = True
            return result

    def rollback(self) -> None:
        """Restore the manifest and only resolution paths prepared here."""
        with self._lock:
            if self.committed or self._rolled_back:
                return
            self._resolution.rollback()
            self._restore_manifest()
            self._rolled_back = True

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

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Rollback every uncommitted exit and preserve exception semantics."""
        if exc is not None or not self.committed:
            self.rollback()
        return False

    def _restore_manifest(self) -> None:
        """Atomically restore the byte-exact manifest snapshot when present."""
        if self._manifest_snapshot is None:
            return
        try:
            self._atomic_restore(self._manifest_snapshot)
            if self._logger is not None:
                self._logger.progress("apm.yml restored to its previous state.")
        except Exception:
            if self._logger is not None:
                self._logger.warning("Failed to restore apm.yml to its previous state.")

    def _atomic_restore(self, snapshot: bytes) -> None:
        """Replace the manifest atomically with *snapshot*."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix="apm-restore-",
            dir=str(self.manifest_path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(snapshot)
            os.replace(temporary_name, self.manifest_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise
