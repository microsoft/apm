"""The no-write oracle's identity stamps see every rewrite (#4790).

``snapshot.capture_stamps`` stamps (inode, mtime_ns, ctime_ns): a byte-identical
rewrite that restores mtime (``os.utime``, ``shutil.copy2`` from a source that
carries the original mtime) still moves the POSIX change time. These controls
keep the converged-install cell
(tests/lifecycle/scenarios/test_converged_install_writes_nothing.py) honest.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from tests.lifecycle.snapshot import capture_stamps, touched

RATCHET_TEST_SCOPE = "repository"

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-idempotency.byte_stable")]


def _rewrite_in_place(path: Path) -> None:
    stat = path.stat()
    path.write_bytes(path.read_bytes())
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _copy2_over(path: Path) -> None:
    """Re-copy from a source that carries the file's own mtime (as a cache copy does)."""
    stat = path.stat()
    source = path.with_name(path.name + ".src")
    source.write_bytes(path.read_bytes())
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    shutil.copy2(source, path)
    source.unlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX change time (st_ctime_ns)")
@pytest.mark.parametrize("rewrite", [_rewrite_in_place, _copy2_over], ids=["utime", "copy2"])
def test_a_metadata_preserving_rewrite_is_still_touched(tmp_path: Path, rewrite) -> None:
    """Negative control: a byte-identical rewrite that restores mtime still counts."""
    path = tmp_path / "apm.lock.yaml"
    path.write_bytes(b"lockfile_version: 1\n")
    before = capture_stamps(tmp_path, ("apm.lock.yaml",))
    time.sleep(0.01)

    rewrite(path)

    after = capture_stamps(tmp_path, ("apm.lock.yaml",))
    assert path.exists() and after["apm.lock.yaml"].mtime_ns == before["apm.lock.yaml"].mtime_ns
    assert touched(before, after) == ["apm.lock.yaml: rewritten"], (before, after)


def test_an_untouched_file_is_not_touched(tmp_path: Path) -> None:
    """Control: reading a file is no write."""
    path = tmp_path / "apm.lock.yaml"
    path.write_bytes(b"lockfile_version: 1\n")
    before = capture_stamps(tmp_path, ("apm.lock.yaml",))

    assert path.read_bytes() and path.exists()

    assert touched(before, capture_stamps(tmp_path, ("apm.lock.yaml",))) == []
