"""The deploy store keeps deployed bytes by canonical digest and verifies every read."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from apm_cli.cache.deploy_store import DEPLOY_BUCKET, DeployStore
from apm_cli.utils.content_hash import canonical_digest, compute_file_hash

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-source.ref_cache_coherent")]

_TEXT = b"---\ndescription: demo\n---\nDo the thing.\n"


def _store(tmp_path: Path) -> tuple[DeployStore, Path]:
    root = tmp_path / "cache" / DEPLOY_BUCKET
    return DeployStore(root), root


def test_the_key_is_the_lockfile_hash_of_the_deployed_file(tmp_path: Path) -> None:
    deployed = tmp_path / "AGENTS.md"
    deployed.write_bytes(b"line one\r\nline two\r\n")
    key = canonical_digest(deployed.read_bytes())

    assert compute_file_hash(deployed) == f"sha256:{key}"


def test_a_stored_entry_reads_back_verified(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)

    assert store.put(key, _TEXT)
    assert store.get(key) == _TEXT
    entry = root / key[:2] / key
    assert entry.read_bytes() == _TEXT
    if sys.platform != "win32":
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert [p.name for p in entry.parent.iterdir()] == [key], "no temp file is left behind"


def test_a_tampered_entry_is_rejected_and_evicted(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    entry = root / key[:2] / key
    entry.write_bytes(_TEXT + b"injected\n")

    assert store.get(key) is None
    assert not entry.exists()
    assert store.put(key, _TEXT) and store.get(key) == _TEXT, "the next full install heals it"


def test_bytes_that_do_not_hash_to_the_key_are_never_stored(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = hashlib.sha256(b"something else").hexdigest()

    assert not store.put(key, _TEXT)
    assert not (root / key[:2] / key).exists()


@pytest.mark.parametrize(
    "key", ["../../etc/passwd", "AB" * 32, "ab" * 31, "ab" * 32 + "\n", "g" * 64, ""]
)
def test_a_key_that_is_not_a_lowercase_hex_digest_is_refused(tmp_path: Path, key: str) -> None:
    store, root = _store(tmp_path)

    assert not store.put(key, _TEXT)
    assert store.get(key) is None
    assert not root.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_linked_entry_or_bucket_is_never_followed(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / key).write_bytes(_TEXT)
    root.mkdir(parents=True)
    (root / key[:2]).symlink_to(outside, target_is_directory=True)

    assert store.get(key) is None
    assert not store.put(key, _TEXT)

    (root / key[:2]).unlink()
    (root / key[:2]).mkdir()
    (root / key[:2] / key).symlink_to(outside / key)
    assert store.get(key) is None
    assert (outside / key).read_bytes() == _TEXT, "the link target is untouched"


def test_a_directory_in_place_of_an_entry_is_not_read(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    (root / key[:2] / key).mkdir(parents=True)

    assert store.get(key) is None
    assert (root / key[:2] / key).is_dir()


def test_no_store_without_a_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APM_NO_CACHE", "1")
    assert DeployStore.open_default() is None
    monkeypatch.delenv("APM_NO_CACHE")
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "c"))
    store = DeployStore.open_default()
    key = canonical_digest(_TEXT)
    assert store is not None and store.put(key, _TEXT)
    assert (tmp_path / "c" / DEPLOY_BUCKET / key[:2] / key).read_bytes() == _TEXT
    assert os.path.isdir(tmp_path / "c")
