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


def test_a_tampered_entry_is_rejected_and_evicted(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    entry = root / key[:2] / key
    entry.write_bytes(_TEXT + b"injected\n")

    assert store.get(key) is None
    assert not entry.exists()
    assert store.put(key, _TEXT) and store.get(key) == _TEXT, "the next full install heals it"


def test_a_read_only_decision_rejects_a_tampered_entry_without_touching_the_store(
    tmp_path: Path,
) -> None:
    from apm_cli.cache.paths import read_only_caches

    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    good = canonical_digest(b"other\n")
    store.put(good, b"other\n")
    entry = root / key[:2] / key
    entry.write_bytes(_TEXT + b"injected\n")
    good_entry = root / good[:2] / good
    os.utime(good_entry, (1, 1))

    with read_only_caches():
        assert store.get(key) is None
        assert store.get(good) == b"other\n"
    assert entry.read_bytes() == _TEXT + b"injected\n"
    assert good_entry.stat().st_mtime == 1


def test_a_read_only_decision_never_creates_the_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.cache.paths import get_cache_root, read_only_caches

    root = tmp_path / "absent-cache"
    monkeypatch.setenv("APM_CACHE_DIR", str(root))
    with read_only_caches():
        assert get_cache_root() == root.resolve() or get_cache_root() == root
    assert not root.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_record_holds_only_under_the_mode_policy_it_was_written_with() -> None:
    """A replay writes plain files, so a different umask finds no record."""
    from apm_cli.cache.deploy_store import package_record_key, plain_file_mode

    ident = ("o/p", "a" * 40, "sha256:" + "b" * 64, ["copilot"], "1.0", "project")
    previous = os.umask(0o022)
    try:
        plain_file_mode.cache_clear()
        written = package_record_key(*ident)
        assert plain_file_mode() == 0o644
        os.umask(0o077)
        plain_file_mode.cache_clear()
        assert plain_file_mode() == 0o600
        assert package_record_key(*ident) != written
    finally:
        os.umask(previous)
        plain_file_mode.cache_clear()


def test_the_umask_is_read_at_most_once_and_before_the_install_starts_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process-wide umask swap races other threads: one read, primed by the transaction."""
    from apm_cli.cache.deploy_store import plain_file_mode
    from apm_cli.install.transaction import InstallTransaction

    swaps: list[int] = []
    real_umask = os.umask

    def counting_umask(mask: int) -> int:
        swaps.append(mask)
        return real_umask(mask)

    monkeypatch.setattr("os.umask", counting_umask)
    plain_file_mode.cache_clear()
    try:
        InstallTransaction(
            manifest_path=tmp_path / "apm.yml",
            apm_modules_dir=tmp_path / "apm_modules",
            validation=None,
            logger=None,
            acquire_lock=False,
        ).rollback()
        assert plain_file_mode.cache_info().currsize == 1, "primed before the pipeline"
        for _ in range(3):
            plain_file_mode()
        assert len(swaps) <= 2, "at most one swap (set and restore), and none on Linux"
    finally:
        plain_file_mode.cache_clear()


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


def _record_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, bytes]):
    from apm_cli.cache.deploy_store import (
        commit_staged,
        package_record_key,
        stage,
        stage_record,
    )
    from apm_cli.core.invocation_memo import invocation_scope

    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "c"))
    ident = (
        "https://github.com/o/r",
        "a" * 40,
        "sha256:" + "b" * 64,
        ["copilot"],
        "9.9",
        "project",
    )
    record_files = {}
    with invocation_scope():
        for rel, data in files.items():
            path = tmp_path / "project" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            digest, raw = canonical_digest(data), hashlib.sha256(data).hexdigest()
            stage(path, digest, raw, data.isascii())
            record_files[rel] = [digest, raw]
        record = {"v": 1, "files": record_files, "ascii_only": True, "body": ["  pkg"]}
        stage_record(package_record_key(*ident), record)
        commit_staged()
    return DeployStore(tmp_path / "c"), ident, record_files


def test_a_package_record_binds_exactly_the_bytes_deployed_for_that_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apm_cli.cache.deploy_store import record_matches

    store, ident, files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT, "b.md": b"two\n"})
    record = store.package_record(*ident)

    assert record is not None and record["files"] == files and record["body"] == ["  pkg"]
    lock = {rel: f"sha256:{digest}" for rel, (digest, _raw) in files.items()}
    assert record_matches(record, lock)
    for rel, (digest, raw) in files.items():
        assert store.get(digest, raw_sha256=raw) == (tmp_path / "project" / rel).read_bytes()


def test_a_lock_edit_pointing_a_path_at_another_cached_blob_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apm_cli.cache.deploy_store import record_matches

    other = b"bytes cached from another project\n"
    store, ident, _files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    store.put(canonical_digest(other), other)
    edited = {"a.md": f"sha256:{canonical_digest(other)}"}

    assert store.get(canonical_digest(other)) == other, "the other blob is in the cache"
    assert not record_matches(store.package_record(*ident), edited)
    assert not record_matches(None, edited)


def test_a_crlf_image_is_refused_by_the_recorded_raw_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, _ident, files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    digest, raw = files["a.md"]
    entry = tmp_path / "c" / DEPLOY_BUCKET / digest[:2] / digest
    entry.write_bytes(_TEXT.replace(b"\n", b"\r\n"))  # same canonical digest

    assert store.get(digest) is not None
    assert store.get(digest, raw_sha256=raw) is None


def test_another_setup_never_reads_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, ident, _files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    repo, commit, content, targets, version, scope = ident

    assert store.package_record(*ident) is not None
    for changed in (
        (repo, "c" * 40, content, targets, version, scope),
        (repo, commit, "sha256:" + "d" * 64, targets, version, scope),
        (repo, commit, content, ["copilot", "claude"], version, scope),
        (repo, commit, content, targets, "9.10", scope),
        (repo, commit, content, targets, version, "user"),
    ):
        assert store.package_record(*changed) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'{"v": 2, "files": {}, "ascii_only": true, "body": null}',
        b'{"v": 1, "files": {"a.md": ["../x", "y"]}, "ascii_only": true, "body": null}',
        b'{"v": 1, "files": {"a.md": ["' + b"a" * 64 + b'"]}, "ascii_only": true, "body": null}',
        b'{"v": 1, "files": {"a.md": ["' + b"a" * 64 + b'", "' + b"b" * 64 + b'"]}, '
        b'"ascii_only": "yes", "body": null}',
    ],
)
def test_a_malformed_record_reads_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes
) -> None:
    from apm_cli.cache.deploy_store import RECORDS_DIR, package_record_key

    store, ident, _files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    key = package_record_key(*ident)
    path = tmp_path / "c" / DEPLOY_BUCKET / RECORDS_DIR / key[:2] / f"{key}.json"
    path.write_bytes(payload)

    assert store.package_record(*ident) is None


@pytest.mark.parametrize(
    "body",
    [
        ["  |-- ok", "\x1b]0;title\x07"],
        ["  |-- bell\a"],
        ["  |-- caf\u00e9"],
        ["x" * 1001],
        ["  |-- line"] * 2001,
    ],
    ids=["escape", "control", "non-ascii", "long-line", "too-many-lines"],
)
def test_a_record_whose_body_is_not_printable_ascii_reads_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: list[str]
) -> None:
    """The body is replayed to the terminal, so only bounded printable ASCII lines are kept."""
    import json

    from apm_cli.cache.deploy_store import RECORDS_DIR, package_record_key

    store, ident, files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    key = package_record_key(*ident)
    path = tmp_path / "c" / DEPLOY_BUCKET / RECORDS_DIR / key[:2] / f"{key}.json"
    record = {"v": 1, "files": files, "ascii_only": True, "body": body}
    path.write_text(json.dumps(record), encoding="ascii")

    assert store.package_record(*ident) is None
    record["body"] = ["  |-- 1 prompt integrated -> .github/prompts/"]
    path.write_text(json.dumps(record), encoding="ascii")
    assert store.package_record(*ident)["body"] == record["body"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_linked_record_is_never_followed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from apm_cli.cache.deploy_store import RECORDS_DIR, package_record_key

    store, ident, _files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT})
    key = package_record_key(*ident)
    path = tmp_path / "c" / DEPLOY_BUCKET / RECORDS_DIR / key[:2] / f"{key}.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    assert store.package_record(*ident) is None
    assert outside.exists()


def test_no_record_is_written_when_one_of_its_blobs_is_not_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    big = b"x" * (MAX_DEPLOY_ENTRY_BYTES + 1)
    store, ident, _files = _record_setup(monkeypatch, tmp_path, {"a.md": _TEXT, "big.bin": big})

    assert store.package_record(*ident) is None
    assert store.get(canonical_digest(_TEXT)) == _TEXT


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


def test_package_records_count_toward_the_cap_and_are_evicted_over_it(tmp_path: Path) -> None:
    from apm_cli.cache.deploy_store import RECORDS_DIR, package_record_key

    store, root = _store(tmp_path)
    key = canonical_digest(_TEXT)
    store.put(key, _TEXT)
    ident = ("https://github.com/o/r", "a" * 40, "sha256:" + "b" * 64, ["copilot"], "9.9", "x")
    raw = hashlib.sha256(_TEXT).hexdigest()
    record = {"v": 1, "files": {"a.md": [key, raw]}, "ascii_only": True, "body": None}
    record_key = package_record_key(*ident)
    assert store.put_record(record_key, record)
    record_path = root / RECORDS_DIR / record_key[:2] / f"{record_key}.json"
    os.utime(record_path, (1000, 1000))
    total = len(_TEXT) + record_path.stat().st_size

    assert store.get_stats()["total_size_bytes"] == total
    assert store.enforce_size_cap(limit=total - 1) == 1, "the older record goes first"
    assert not record_path.exists()
    assert store.package_record(*ident) is None, "an evicted record is a miss"
    assert store.get(key) == _TEXT


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
