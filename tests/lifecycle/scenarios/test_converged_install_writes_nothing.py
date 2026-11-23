"""A converged install writes nothing, not even byte-identical bytes (#4790).

PROMISE: P-LAW-idempotency.byte_stable -- repeating a converged operation leaves
durable bytes unchanged; P-INV-I2 -- idempotence (source: CHANGELOG 0.38.0
"Fixed": a converged ``apm install`` no longer rewrites identical
``apm_modules/*/.apm-pin`` cache markers, so a no-op install writes nothing).

Every digest-based no-write oracle is blind to a rewrite of identical bytes, so
this cell compares identity stamps (inode, mtime_ns, ctime_ns; ``snapshot.capture_stamps``)
of the cache markers, the lock and every deployed file across a repeated install.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from tests.lifecycle import driver
from tests.lifecycle.snapshot import capture_stamps, touched
from tests.lifecycle.world import World

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-LAW-idempotency.byte_stable", "P-INV-I2"),
]

_PINS = ("apm_modules/**/.apm-pin",)
_DURABLE = ("apm.lock.yaml", ".github/**/*", ".agents/**/*", ".claude/**/*", "AGENTS.md")


def _ok(world: World, *argv: str) -> str:
    result = driver.run(world, argv)
    assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
    return result.stdout + result.stderr


@pytest.mark.parametrize("scope", ["cache-markers", "lock-and-deployed"])
def test_a_converged_install_touches_no_file(lifecycle_world: World, scope: str) -> None:
    world = lifecycle_world
    _ok(world, "install", world.packages["p-skill:git"].spec)
    _ok(world, "install")
    patterns = _PINS if scope == "cache-markers" else _DURABLE
    before = capture_stamps(world.project, patterns)
    assert before, f"precondition: nothing matched {patterns}"

    result = driver.run(world, ("install",))

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    rewritten = touched(before, capture_stamps(world.project, patterns))
    assert not rewritten, "a converged install wrote:\n" + "\n".join(rewritten) + f"\n{output}"


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
