"""The content hash reuses this command's verified file digests and never a stale one (#4939).

The parity check (``APM_DEBUG_HASH_MEMO=1``) fails loudly on a wrong digest;
an APM writer and an outside edit are both seen; on Windows, where the file
identity cannot see a same-size edit with restored times, only APM's own
writers invalidate (the killed control).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import file_digest_memo
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = [
    pytest.mark.unit,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-package-content-hash"),
]


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / ".apm" / "skills" / "a").mkdir(parents=True)
    (root / "apm.yml").write_bytes(b"name: pkg\nversion: 1.0.0\n")
    (root / ".apm" / "skills" / "a" / "SKILL.md").write_bytes(b"# A\nbody\n")
    return root


def _record_tree(root: Path) -> None:
    """What the checkout verifier does for every copied file it proves."""
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        file_digest_memo.record(path, os.lstat(path), path.read_bytes())


def test_a_memoized_hash_equals_a_full_read_and_skips_nothing_it_should_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _package(tmp_path)
    full = compute_package_hash(root)
    monkeypatch.setenv("APM_DEBUG_HASH_MEMO", "1")  # every hit is re-read and compared
    with invocation_scope():
        _record_tree(root)
        memoized = compute_package_hash(root)
        (root / "new.md").write_bytes(b"added after verification\n")
        grown = compute_package_hash(root)
    assert memoized == full
    assert grown == compute_package_hash(root)


def test_the_parity_check_fails_on_a_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _package(tmp_path)
    target = root / "apm.yml"
    monkeypatch.setenv("APM_DEBUG_HASH_MEMO", "1")
    with invocation_scope():
        file_digest_memo.record(target, os.lstat(target), b"not these bytes")
        with pytest.raises(file_digest_memo.DigestMemoMismatch):
            compute_package_hash(root)


def test_an_apm_post_copy_edit_is_hashed_from_its_new_bytes(tmp_path: Path) -> None:
    root = _package(tmp_path)
    with invocation_scope():
        _record_tree(root)
        # stamp_plugin_version-style rewrite: same size, through APM's writer.
        dump_yaml({"name": "pkg", "version": "9.0.0"}, root / "apm.yml")
        memoized = compute_package_hash(root)
    assert memoized == compute_package_hash(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ctime moves on any content change")
def test_an_outside_same_size_edit_with_restored_times_is_detected(tmp_path: Path) -> None:
    root = _package(tmp_path)
    target = root / ".apm" / "skills" / "a" / "SKILL.md"
    with invocation_scope():
        _record_tree(root)
        before = os.lstat(target)
        with open(target, "r+b") as handle:
            handle.write(b"# B")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        memoized = compute_package_hash(root)
    assert target.read_bytes() == b"# B\nbody\n"
    assert memoized == compute_package_hash(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows ctime is creation time")
def test_killed_control_on_windows_only_apm_writers_invalidate(tmp_path: Path) -> None:
    """Mutation control: an edit that bypasses APM's writers keeps the old digest."""
    root = _package(tmp_path)
    target = root / ".apm" / "skills" / "a" / "SKILL.md"
    with invocation_scope():
        _record_tree(root)
        before = os.lstat(target)
        with open(target, "r+b") as handle:
            handle.write(b"# B")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        stale = compute_package_hash(root)
    assert target.read_bytes() == b"# B\nbody\n"
    assert stale != compute_package_hash(root)
