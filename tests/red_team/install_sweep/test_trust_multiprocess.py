"""Vector 5 -- trust store under REAL multiprocessing (attack the fcntl lock).

Round-1 only exercised THREADS. The thread lock alone cannot defend against
separate OS processes; only the fcntl advisory lock can. Here we fork real
processes that hammer trust()/untrust() of distinct paths while a separate
reader process loops over the store -- the reader must NEVER parse a torn /
partial / invalid JSON, and NO write may be lost.

Also: crash a writer mid-write and assert the store stays valid (atomic
os.replace) with no corruption, and a leftover temp file does not break the
next read.

These run module-level workers so they survive both fork and spawn start
methods.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

from apm_cli.core.script_trust import (
    _load_trust_store,
    _trust_store_path,
    is_project_scripts_trusted,
    trust_project_scripts,
)

from .conftest import write_project

_IS_POSIX = os.name == "posix"


# -- module-level workers (picklable for spawn) ----------------------------


def _writer_proc(apm_home: str, apm_yml: str, rounds: int) -> None:
    os.environ["APM_HOME"] = apm_home
    from apm_cli.core.script_trust import (
        trust_project_scripts as _trust,
    )
    from apm_cli.core.script_trust import (
        untrust_project_scripts as _untrust,
    )

    p = Path(apm_yml)
    for _ in range(rounds):
        _trust(p)
        _untrust(p)
    _trust(p)  # leave it trusted at the end


def _trust_once_proc(apm_home: str, apm_yml: str) -> None:
    os.environ["APM_HOME"] = apm_home
    from apm_cli.core.script_trust import trust_project_scripts as _trust

    _trust(Path(apm_yml))


def _reader_proc(apm_home: str, store_path: str, rounds: int, err_q: mp.Queue) -> None:
    os.environ["APM_HOME"] = apm_home
    p = Path(store_path)
    errors = 0
    for _ in range(rounds):
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            continue  # file briefly absent during replace window is fine
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            errors += 1  # TORN READ -- the atomic-replace guarantee broke
            continue
        if not (isinstance(data, dict) and "projects" in data):
            errors += 1
    err_q.put(errors)


def _crash_mid_write_proc(apm_home: str, apm_yml: str) -> None:
    """Begin a trust write but crash exactly at os.replace, leaving the
    real store untouched (atomicity check)."""
    os.environ["APM_HOME"] = apm_home
    import apm_cli.core.script_trust as st

    real_replace = os.replace

    def _boom(src, dst):
        # tmp file is fully written by now; die before it is swapped in.
        os._exit(7)

    os.replace = _boom  # type: ignore[assignment]
    try:
        st.trust_project_scripts(Path(apm_yml))
    finally:  # pragma: no cover - process is expected to have exited
        os.replace = real_replace
        os._exit(0)


# -- tests -----------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not _IS_POSIX, reason="fcntl/fork multiprocess attack is POSIX-only")
def test_multiprocess_distinct_trusts_no_lost_update_no_torn_read(
    apm_home: Path, tmp_path: Path
) -> None:
    """N separate processes trust distinct projects + a reader loops the
    store: every record must persist and the reader must never see torn JSON.
    """
    n = 16
    apm_ymls = [
        str(write_project(tmp_path / f"proj{i}", "post-install", [f"echo proj{i}"]))
        for i in range(n)
    ]
    store = str(_trust_store_path())
    # Seed the store so the reader has something to read from the start.
    trust_project_scripts(Path(apm_ymls[0]))

    ctx = mp.get_context("fork")
    err_q: mp.Queue = ctx.Queue()
    reader = ctx.Process(target=_reader_proc, args=(str(apm_home), store, 400, err_q))
    reader.start()

    writers = [ctx.Process(target=_trust_once_proc, args=(str(apm_home), y)) for y in apm_ymls]
    for w in writers:
        w.start()
    for w in writers:
        w.join(timeout=30)
    reader.join(timeout=30)

    torn = err_q.get(timeout=5) if not err_q.empty() else 0
    assert torn == 0, f"reader observed {torn} torn/partial trust-store reads"

    persisted = _load_trust_store()
    expected_keys = {str(Path(y).resolve()) for y in apm_ymls}
    missing = expected_keys - set(persisted.keys())
    assert not missing, (
        f"LOST UPDATE across processes: {len(missing)}/{n} trust records "
        "missing -- the fcntl advisory lock failed to serialise writers."
    )


@pytest.mark.slow
@pytest.mark.skipif(not _IS_POSIX, reason="fcntl/fork multiprocess attack is POSIX-only")
def test_multiprocess_trust_untrust_churn_keeps_store_valid(apm_home: Path, tmp_path: Path) -> None:
    """Concurrent trust/untrust churn across processes + reader -> always
    valid JSON, no corruption."""
    apm_ymls = [
        str(write_project(tmp_path / f"c{i}", "post-install", [f"echo c{i}"])) for i in range(6)
    ]
    store = str(_trust_store_path())
    trust_project_scripts(Path(apm_ymls[0]))

    ctx = mp.get_context("fork")
    err_q: mp.Queue = ctx.Queue()
    reader = ctx.Process(target=_reader_proc, args=(str(apm_home), store, 600, err_q))
    reader.start()
    writers = [ctx.Process(target=_writer_proc, args=(str(apm_home), y, 15)) for y in apm_ymls]
    for w in writers:
        w.start()
    for w in writers:
        w.join(timeout=30)
    reader.join(timeout=30)

    torn = err_q.get(timeout=5) if not err_q.empty() else 0
    assert torn == 0, f"reader saw {torn} torn reads under trust/untrust churn"
    # Final store must still parse and be schema-valid.
    data = json.loads(Path(store).read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "projects" in data


@pytest.mark.slow
@pytest.mark.skipif(not _IS_POSIX, reason="fork required to crash a writer mid-write")
def test_writer_crash_mid_write_leaves_store_intact(apm_home: Path, tmp_path: Path) -> None:
    """A writer crashing exactly at os.replace must not corrupt the store
    nor have a leftover temp break the next read."""
    yml_a = str(write_project(tmp_path / "a", "post-install", ["echo a"]))
    yml_b = str(write_project(tmp_path / "b", "post-install", ["echo b"]))
    # Establish a known-good store containing only project A.
    trust_project_scripts(Path(yml_a))
    before = _load_trust_store()
    assert str(Path(yml_a).resolve()) in before

    ctx = mp.get_context("fork")
    crasher = ctx.Process(target=_crash_mid_write_proc, args=(str(apm_home), yml_b))
    crasher.start()
    crasher.join(timeout=15)
    assert crasher.exitcode == 7, "crasher did not die at the injected os.replace"

    # Store must be byte-identical valid JSON: B was never swapped in.
    after = _load_trust_store()
    assert after == before, "store mutated despite the write crashing pre-replace"
    assert str(Path(yml_b).resolve()) not in after

    # A leftover .tmp may exist but MUST NOT break a subsequent real write.
    leftovers = list(Path(_trust_store_path()).parent.glob(".scripts-trust.*.tmp"))
    trust_project_scripts(Path(yml_b))  # must succeed
    final = _load_trust_store()
    assert str(Path(yml_b).resolve()) in final
    assert str(Path(yml_a).resolve()) in final
    # Record (not fail) whether a temp leaked, to inform the report.
    sys.stderr.write(f"[observation] leftover temp files after crash: {len(leftovers)}\n")


@pytest.mark.skipif(not _IS_POSIX, reason="chmod-based perms are POSIX-only")
def test_gate_read_never_raises_on_unreadable_store(apm_home: Path, tmp_path: Path) -> None:
    """If the store path is unreadable (e.g. it's a directory), the GATE
    read must fail safe (untrusted), never raise into install."""
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    # Make the store path a directory so read_text raises OSError.
    store.mkdir()
    yml = write_project(tmp_path / "p", "post-install", ["echo hi"])
    # Must return False, not raise.
    assert is_project_scripts_trusted(yml) is False
    assert _load_trust_store() == {}
