"""The deploy store keeps deployed bytes by canonical digest and verifies every read."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from apm_cli.cache.deploy_store import (
    DEPLOY_BUCKET,
    MAX_DEPLOY_ENTRY_BYTES,
    DeployStore,
)
from apm_cli.utils.content_hash import canonical_digest, compute_file_hash

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-source.ref_cache_coherent")]

_TEXT = b"---\ndescription: demo\n---\nDo the thing.\n"


def _store(tmp_path: Path) -> tuple[DeployStore, Path]:
    return DeployStore(tmp_path / "cache"), tmp_path / "cache" / DEPLOY_BUCKET


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


def test_a_tampered_entry_is_rejected_and_evicted_on_read(tmp_path: Path) -> None:
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


def test_an_exact_byte_reader_never_gets_the_other_line_ending_image(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    crlf = _TEXT.replace(b"\n", b"\r\n")
    key = canonical_digest(_TEXT)
    assert canonical_digest(crlf) == key
    store.put(key, crlf)

    assert store.get(key) == crlf
    assert store.get(key, raw_sha256=hashlib.sha256(_TEXT).hexdigest()) is None
    assert store.get(key, raw_sha256=hashlib.sha256(crlf).hexdigest()) == crlf
    assert (root / key[:2] / key).read_bytes() == crlf, "a raw mismatch is a miss, not an eviction"


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_the_store_follows_the_cache_root_no_cache_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("APM_NO_CACHE", value)
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "c"))
    store = DeployStore.open_default()
    key = canonical_digest(_TEXT)

    assert store is not None and store.put(key, _TEXT)
    assert (tmp_path / "c" / DEPLOY_BUCKET / key[:2] / key).read_bytes() == _TEXT


def test_deployed_bytes_wait_for_the_install_to_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apm_cli.cache.deploy_store import commit_staged, discard_staged, stage
    from apm_cli.core.invocation_memo import invocation_scope

    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "c"))
    deployed = tmp_path / "AGENTS.md"
    deployed.write_bytes(_TEXT)
    key = canonical_digest(_TEXT)
    entry = tmp_path / "c" / DEPLOY_BUCKET / key[:2] / key

    stage(deployed, key)
    assert commit_staged() == 0, "outside a command run nothing is staged"
    with invocation_scope():
        stage(deployed, key)
        assert not entry.exists(), "hashing alone stores nothing"
        discard_staged()
        assert commit_staged() == 0
        stage(deployed, key)
        assert commit_staged() == 1
    assert entry.read_bytes() == _TEXT


def test_bytes_that_changed_after_hashing_are_not_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apm_cli.cache.deploy_store import commit_staged, stage
    from apm_cli.core.invocation_memo import invocation_scope

    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "c"))
    deployed = tmp_path / "AGENTS.md"
    deployed.write_bytes(_TEXT)
    key = canonical_digest(_TEXT)
    with invocation_scope():
        stage(deployed, key)
        deployed.write_bytes(_TEXT + b"changed after hashing\n")
        assert commit_staged() == 0
    assert not (tmp_path / "c" / DEPLOY_BUCKET / key[:2] / key).exists()


def test_the_store_evicts_least_recently_used_entries_above_its_bound(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    blobs = [f"entry {n}\n".encode() * 100 for n in range(5)]
    keys = [canonical_digest(blob) for blob in blobs]
    for age, (key, blob) in enumerate(zip(keys, blobs, strict=True)):
        store.put(key, blob)
        os.utime(root / key[:2] / key, (1000 + age, 1000 + age))
    store.get(keys[0])  # a verified read refreshes recency
    limit = sum(len(blob) for blob in blobs[:3])

    assert store.enforce_size_cap(limit) == 3
    kept = {path.name for path in root.rglob("*") if path.is_file()}
    assert kept == {keys[0], keys[4]}


def test_an_entry_above_the_entry_bound_is_never_stored(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    big = b"x" * (MAX_DEPLOY_ENTRY_BYTES + 1)
    key = canonical_digest(big)
    source = tmp_path / "big.bin"
    source.write_bytes(big)

    assert not store.put(key, big)
    assert not store.put_file(key, source)
    assert not (root / key[:2] / key).exists()


def test_cache_clean_empties_the_store(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)

    assert store.clean_all() == []
    assert list(root.iterdir()) == []
    assert DeployStore(tmp_path / "never-used").clean_all() == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_fifo_at_an_entry_path_is_a_miss_not_a_hang(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    (root / key[:2]).mkdir(parents=True)
    os.mkfifo(root / key[:2] / key)

    assert store.get(key) is None
    assert stat.S_ISFIFO(os.lstat(root / key[:2] / key).st_mode), "the FIFO was only refused"


def test_a_same_size_tampered_entry_is_healed_by_the_next_store(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    entry = root / key[:2] / key
    entry.write_bytes(bytes(reversed(_TEXT)))  # same length, other bytes

    assert store.put(key, _TEXT)
    assert entry.read_bytes() == _TEXT
    entry.write_bytes(bytes(reversed(_TEXT)))
    source = tmp_path / "deployed.md"
    source.write_bytes(_TEXT)
    assert store.put_file(key, source)
    assert entry.read_bytes() == _TEXT


def test_same_length_bytes_for_another_digest_are_refused(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)

    assert not store.put(key, bytes(reversed(_TEXT)))
    assert (root / key[:2] / key).read_bytes() == _TEXT


def test_an_entry_grown_past_the_bound_is_a_miss_and_evicted(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    entry = root / key[:2] / key
    with entry.open("ab") as handle:
        handle.truncate(MAX_DEPLOY_ENTRY_BYTES + 1)

    assert store.get(key) is None
    assert not entry.exists()


def test_eviction_touches_only_key_named_entries_in_their_shard(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    shard = root / key[:2]
    strays = [shard / ".tmp-abc.part", shard / "notes.txt", root / "ff" / key]
    for stray in strays:
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"x" * 4096)

    assert store.get_stats() == {"entry_count": 1, "total_size_bytes": len(_TEXT)}
    assert store.enforce_size_cap(limit=1) == 1
    assert not (shard / key).exists()
    assert all(stray.read_bytes() == b"x" * 4096 for stray in strays)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_eviction_never_follows_a_linked_shard(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / key).write_bytes(_TEXT)
    root.mkdir(parents=True)
    (root / key[:2]).symlink_to(outside, target_is_directory=True)

    assert store.enforce_size_cap(limit=0) == 0
    assert (outside / key).read_bytes() == _TEXT
