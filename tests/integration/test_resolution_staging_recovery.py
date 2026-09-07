"""Cross-process coverage for interrupted-install staging recovery."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.install.transaction import InstallTransaction
from apm_cli.models.results import InstallResult

pytestmark = pytest.mark.component

_LOCK_HOLDER = """
import sys
import time
from pathlib import Path
from filelock import FileLock

lock = FileLock(sys.argv[1])
with lock:
    Path(sys.argv[2]).touch()
    time.sleep(30)
"""


def test_successful_install_preserves_staging_locked_by_another_process(
    tmp_path: Path,
) -> None:
    """A successful install cannot delete another process's active backup."""
    modules = tmp_path / "apm_modules"
    staging_parent = modules / ".apm-resolution-staging"
    active = staging_parent / ("a" * 32)
    (active / "package").mkdir(parents=True)
    marker = active / "package" / "marker"
    marker.write_text("original", encoding="ascii")
    lock_path = active.with_suffix(".lock")
    ready = tmp_path / "lock-ready"
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "lock-holder process did not acquire the staging lock"

        transaction = InstallTransaction(
            manifest_path=tmp_path / "apm.yml",
            apm_modules_dir=modules,
            validation=None,
            logger=MagicMock(),
        )
        transaction.commit(InstallResult())

        assert marker.read_text(encoding="ascii") == "original"
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_successful_install_does_not_report_live_lock_without_staging_root(
    tmp_path: Path,
) -> None:
    """A lock holder is active even before it creates its staging directory."""
    modules = tmp_path / "apm_modules"
    staging_parent = modules / ".apm-resolution-staging"
    staging_parent.mkdir(parents=True)
    lock_path = staging_parent / f"{'c' * 32}.lock"
    ready = tmp_path / "lock-only-ready"
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "lock-holder process did not acquire the staging lock"
        logger = MagicMock()
        transaction = InstallTransaction(
            manifest_path=tmp_path / "apm.yml",
            apm_modules_dir=modules,
            validation=None,
            logger=logger,
        )

        transaction.commit(InstallResult())

        logger.warning.assert_not_called()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_successful_install_preserves_symlinked_staging_entry(tmp_path: Path) -> None:
    """Cleanup never follows a staging candidate symlink."""
    modules = tmp_path / "apm_modules"
    staging_parent = modules / ".apm-resolution-staging"
    staging_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_marker = outside / "marker"
    outside_marker.write_text("keep", encoding="ascii")
    candidate = staging_parent / ("b" * 32)
    candidate.symlink_to(outside, target_is_directory=True)
    candidate.with_suffix(".lock").write_text("", encoding="ascii")
    logger = MagicMock()
    transaction = InstallTransaction(
        manifest_path=tmp_path / "apm.yml",
        apm_modules_dir=modules,
        validation=None,
        logger=logger,
    )

    transaction.commit(InstallResult())

    assert candidate.is_symlink()
    assert outside_marker.read_text(encoding="ascii") == "keep"
    logger.warning.assert_not_called()
