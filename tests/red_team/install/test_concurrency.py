"""Vector 4 -- concurrency: log integrity + trust-store races.

Two attacks:

1. Concurrent fires of the SAME event must not garble the shared
   ~/.apm/logs/scripts.log (each append must stay intact). Regression
   trap: O_APPEND single-write blocks should not interleave.

2. The trust store read-modify-writes ($APM_HOME/scripts-trust.json) are
   NOT atomic. Concurrent trust() calls for distinct projects can lose
   updates (last-writer-wins on a stale snapshot), and a reader can
   momentarily observe a half-written file. We assert the SECURE
   invariants (no lost updates; store always parses) so any torn/lost
   write surfaces as a failure.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
)
from apm_cli.core.script_trust import (
    is_project_scripts_trusted,
    trust_project_scripts,
    untrust_project_scripts,
)

from .conftest import PYEXE, make_command_entry, trust, write_project


def _event(wd: str) -> LifecycleEvent:
    return LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=wd,
    )


def test_concurrent_fires_do_not_corrupt_log(apm_home: Path, tmp_path: Path) -> None:
    """Two runners firing concurrently keep every log block intact."""
    n_per = 8
    markers = [f"RTLOGMARKER{i:03d}" for i in range(2 * n_per)]

    def make_runner(chunk: list[str]) -> LifecycleScriptRunner:
        scripts = [make_command_entry(f'{PYEXE} -c "print(chr(120))" && echo {m}') for m in chunk]
        return LifecycleScriptRunner(scripts=scripts)

    r1 = make_runner(markers[:n_per])
    r2 = make_runner(markers[n_per:])

    def fire(r: LifecycleScriptRunner) -> None:
        r.fire("post-install", _event(str(tmp_path)))

    t1 = threading.Thread(target=fire, args=(r1,))
    t2 = threading.Thread(target=fire, args=(r2,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    log = apm_home / "logs" / "scripts.log"
    assert log.is_file(), "scripts.log was never created"
    text = log.read_text(encoding="utf-8")

    headers = [ln for ln in text.splitlines() if ln.startswith("[") and "event=" in ln]
    assert len(headers) == 2 * n_per, f"expected {2 * n_per} log blocks, found {len(headers)}"
    for m in markers:
        assert f"stdout: {m}" in text or m in text, f"marker {m} missing/garbled"
    # No header line may contain two distinct markers (torn interleave).
    for ln in text.splitlines():
        hits = [m for m in markers if m in ln]
        assert len(hits) <= 1, f"interleaved log line mixes markers: {ln!r}"


def test_trust_store_stays_valid_json_under_concurrency(apm_home: Path, tmp_path: Path) -> None:
    """trust/untrust/is_trusted in parallel must leave a parseable store."""
    apm_yml = write_project(tmp_path / "p", "post-install", ["echo hi"])
    barrier = threading.Barrier(3)

    def truster() -> None:
        barrier.wait()
        for _ in range(50):
            trust_project_scripts(apm_yml)

    def untruster() -> None:
        barrier.wait()
        for _ in range(50):
            untrust_project_scripts(apm_yml)

    def reader() -> None:
        barrier.wait()
        for _ in range(50):
            # Must never raise even if it reads a half-written file.
            is_project_scripts_trusted(apm_yml)

    threads = [
        threading.Thread(target=truster),
        threading.Thread(target=untruster),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    store = apm_home / "scripts-trust.json"
    if store.exists():
        data = json.loads(store.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and "projects" in data, "store schema broke"


def test_concurrent_trust_no_lost_updates(apm_home: Path, tmp_path: Path) -> None:
    """N distinct projects trusted concurrently -> ALL must persist.

    The store does load -> mutate -> write_text without a lock, so a
    stale-snapshot writer can clobber a peer's entry (lost update). The
    secure invariant is that every distinct project survives.
    """
    n = 24
    apm_ymls = [
        write_project(tmp_path / f"proj{i}", "post-install", [f"echo proj{i}"]) for i in range(n)
    ]
    barrier = threading.Barrier(n)

    def worker(path: Path) -> None:
        barrier.wait()  # maximise contention on the read-modify-write window
        trust_project_scripts(path)

    threads = [threading.Thread(target=worker, args=(p,)) for p in apm_ymls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    store = apm_home / "scripts-trust.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    persisted = data.get("projects", {})
    assert len(persisted) == n, (
        f"LOST UPDATE: only {len(persisted)}/{n} trust records survived "
        "concurrent trust() -- store read-modify-write is unsynchronised."
    )


def test_trusted_value_is_consistent_after_race(apm_home: Path, tmp_path: Path) -> None:
    """After a settling trust(), the gate reads the project as trusted."""
    apm_yml = write_project(tmp_path / "p", "post-install", ["echo hi"])
    trust(apm_yml)
    assert is_project_scripts_trusted(apm_yml)
