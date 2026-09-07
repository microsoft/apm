"""Filesystem proofs for verified cache cleanup and its public consumers."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from apm_cli.cache.cleanup import clean_cache_buckets
from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.http_cache import HttpCache

pytestmark = pytest.mark.component


@pytest.mark.parametrize("cache_type", [GitCache, HttpCache])
def test_clean_all_reports_file_denial_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache_type: type[GitCache] | type[HttpCache]
) -> None:
    """Unlink failures report their cause and do not prevent sibling cleanup."""
    cache = cache_type(tmp_path)
    bucket = tmp_path / ("git/db_v1" if cache_type is GitCache else "http_v1")
    denied, removable = bucket / "denied", bucket / "removable"
    denied.write_bytes(b"locked")
    removable.write_bytes(b"remove")
    original = os.unlink

    def unlink(path: str | Path, *args: object, **kwargs: object) -> None:
        if Path(path) == denied:
            raise PermissionError(errno.EACCES, "locked by another process", str(path))
        original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        failures = cache.clean_all()
    assert len(failures) == 1
    assert str(denied) in failures[0]
    assert "locked by another process" in failures[0]
    assert denied.read_bytes() == b"locked"
    assert not removable.exists()
    assert cache.clean_all() == []
    assert not denied.exists()


def test_clean_reports_unreadable_bucket_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enumeration failure is an incomplete result, not an empty cache."""
    denied, healthy = tmp_path / "denied", tmp_path / "healthy"
    denied.mkdir()
    healthy.mkdir()
    (denied / "file").write_bytes(b"keep")
    (healthy / "file").write_bytes(b"remove")
    original = os.scandir

    def scandir(path: str | Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if Path(path) == denied:
            raise PermissionError(errno.EACCES, "cannot enumerate", str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    failures = clean_cache_buckets((denied, healthy))
    assert len(failures) == 1
    assert "cannot enumerate" in failures[0]
    assert (denied / "file").read_bytes() == b"keep"
    assert list(healthy.iterdir()) == []


def test_clean_does_not_follow_entry_or_bucket_symlinks(tmp_path: Path) -> None:
    """Remove entry links themselves but never traverse linked buckets."""
    bucket, outside = tmp_path / "bucket", tmp_path / "outside"
    bucket.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"preserve")
    (bucket / "link").symlink_to(outside, target_is_directory=True)
    (bucket / "dangling").symlink_to(tmp_path / "missing")
    linked_bucket = tmp_path / "linked-bucket"
    linked_bucket.symlink_to(outside, target_is_directory=True)
    assert clean_cache_buckets((bucket,)) == []
    failures = clean_cache_buckets((linked_bucket,))
    assert failures == [f"{linked_bucket}: refusing to traverse a symlinked cache bucket"]
    assert (outside / "file").read_bytes() == b"preserve"
    assert list(bucket.iterdir()) == []


def test_clean_missing_bucket_is_already_clean(tmp_path: Path) -> None:
    """Concurrent removal or a never-created cache needs no cleanup."""
    assert clean_cache_buckets((tmp_path / "missing",)) == []
