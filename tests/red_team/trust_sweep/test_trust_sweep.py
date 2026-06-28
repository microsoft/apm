"""Adversarial red-team sweep of the lifecycle-scripts TRUST subsystem.

Each test asserts the SECURE expectation, so a genuine break makes the
test FAIL. Tests that PASS document an area probed and found secure.

Run:
    uv run --extra dev pytest tests/red_team/trust_sweep/ -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def _write_apm_yml(root: Path, body: str) -> Path:
    p = root / "apm.yml"
    p.write_text(body, encoding="utf-8")
    return p


def _isolate_apm_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "apmhome"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APM_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# VECTOR 1 (GENUINE BREAK): non-JSON-serializable YAML scalar in lifecycle:
# crashes the firing gate before it can fail-closed.
# ---------------------------------------------------------------------------


def test_yaml_date_in_lifecycle_does_not_crash_firing_gate(monkeypatch, tmp_path):
    """A YAML date/timestamp anywhere in lifecycle: must not crash apm.

    The round-1 hardening promised a malformed/hostile manifest degrades
    gracefully (no crash). YAML safe_load turns an unquoted date scalar
    into datetime.date, which json.dumps() in fingerprint_lifecycle_subtree
    cannot serialise. The TypeError is UNCAUGHT and propagates straight out
    of build_runner_from_context -> apm install/update/uninstall crash (DoS),
    and it happens BEFORE the trust gate can skip the untrusted scripts.

    SECURE expectation: build_runner_from_context returns a runner with the
    untrusted project scripts dropped (fail-closed), never raises.
    """
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    _write_apm_yml(
        proj,
        """
lifecycle:
  post-install:
    - type: command
      run: echo hi
      description: 2024-01-01
""",
    )

    from apm_cli.core.lifecycle_scripts import build_runner_from_context

    runner = build_runner_from_context(project_root=str(proj))
    # Untrusted clone -> project scripts must be skipped, not executed,
    # and certainly the call must not raise.
    assert runner.scripts_for_event("post-install") == []


def test_yaml_set_in_lifecycle_does_not_crash_trust_command(monkeypatch, tmp_path):
    """A YAML !!set (-> Python set) is also non-JSON-serializable.

    trust_project_scripts() must degrade (return None / fail to record)
    rather than raise TypeError out of the CLI.
    """
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    yml = _write_apm_yml(
        proj,
        """
lifecycle:
  post-install:
    - type: command
      run: echo hi
      env: !!set
        ? AAA
        ? BBB
""",
    )

    from apm_cli.core.script_trust import trust_project_scripts

    # Must not raise; returning None (could not fingerprint) is acceptable.
    result = trust_project_scripts(yml)
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# VECTOR 1 (SECURE): canonicalization properties
# ---------------------------------------------------------------------------


def test_key_reordering_yields_same_fingerprint():
    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    a = {"post-install": [{"type": "command", "run": "x"}], "pre-install": []}
    b = {"pre-install": [], "post-install": [{"run": "x", "type": "command"}]}
    assert fingerprint_lifecycle_subtree(a) == fingerprint_lifecycle_subtree(b)


def test_lifecycle_edit_rearms_trust(monkeypatch, tmp_path):
    """A security-relevant edit to the executed command changes the fingerprint."""
    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    good = {"post-install": [{"type": "command", "run": "echo safe"}]}
    evil = {"post-install": [{"type": "command", "run": "rm -rf /"}]}
    assert fingerprint_lifecycle_subtree(good) != fingerprint_lifecycle_subtree(evil)


def test_nfc_nfd_keys_do_not_collide():
    """NFC vs NFD byte-different keys must not share a fingerprint.

    Different bytes -> different fingerprint -> editing re-arms (fail-closed).
    """
    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    nfc = {"caf\u00e9": [{"type": "command", "run": "x"}]}
    nfd = {"cafe\u0301": [{"type": "command", "run": "x"}]}
    assert fingerprint_lifecycle_subtree(nfc) != fingerprint_lifecycle_subtree(nfd)


# ---------------------------------------------------------------------------
# VECTOR (GENUINE BREAK): a YAML alias bomb in the lifecycle: subtree is a
# compact in-memory DAG of shared references, but json.dumps() re-serialises
# each reference once per edge -- an exponential tree-expansion that OOM-kills
# the process MID-serialise (so a try/except around json.dumps cannot save
# it). fingerprint_lifecycle_subtree must detect the over-budget expansion
# and fail closed (return None -> untrusted -> scripts skipped) cheaply.
# ---------------------------------------------------------------------------


def _alias_bomb_subtree(width: int = 9, depth: int = 12) -> dict:
    """Build a billion-laughs DAG the way yaml.safe_load(anchors) would.

    Each level is a list holding *width* references to the SAME lower
    level, so the structure is tiny in memory but expands to width**depth
    nodes as a tree. With width=9, depth=12 that is ~2.8e11 tree nodes --
    enough to exhaust memory inside json.dumps if left unguarded.
    """
    node: object = "lol"
    for _ in range(depth):
        node = [node] * width
    return {"post-install": [{"type": "command", "run": node}]}


def test_alias_bomb_fingerprint_fails_closed_fast():
    import time

    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    bomb = _alias_bomb_subtree()
    start = time.monotonic()
    result = fingerprint_lifecycle_subtree(bomb)
    elapsed = time.monotonic() - start

    # Fail-closed: un-fingerprintable -> None -> the project is untrusted
    # and its scripts are skipped (the safe direction).
    assert result is None
    # And it must bail on the node budget, not grind through the expansion:
    # a real OOM attempt would take many seconds (or never return).
    assert elapsed < 2.0, f"fingerprint took {elapsed:.2f}s -- budget guard not bailing early"


def test_is_fingerprint_safe_bounds_expansion():
    from apm_cli.core.script_trust import _is_fingerprint_safe

    # A small legitimate subtree passes the structural safety check.
    legit = {"post-install": [{"type": "command", "run": "echo hi"}]}
    assert _is_fingerprint_safe(legit) is True

    # The alias bomb reuses a container reference -> rejected.
    bomb = _alias_bomb_subtree()
    assert _is_fingerprint_safe(bomb) is False


# ---------------------------------------------------------------------------
# VECTOR (ROUND-3 GENUINE BREAK): the node-COUNT-only guard was the wrong
# metric. Three distinct alias/nesting shapes defeat a pure node-count cap and
# OOM or crash json.dumps -- each must fail closed (return None) FAST. The fix
# is a structural check that rejects any reused container reference, over-deep
# nesting, or oversized serialised byte total before json.dumps runs.
# ---------------------------------------------------------------------------


def _high_fanout_self_alias(fanout: int = 50_000) -> dict:
    """A single list that references ITSELF *fanout* times.

    yaml.safe_load of ``a: &a [*a, *a, ...]`` decodes to a list whose every
    element is the same list object. A node-count walk that pushes each child
    without id-dedup grows its explicit stack to ~fanout per pop and explodes;
    the container-id guard rejects on the second visit so the stack stays
    bounded by a single fan-out width.
    """
    node: list = []
    node.extend([node] * fanout)
    return {"post-install": [{"type": "command", "run": node}]}


def _fat_scalar_aliased(reps: int = 4000, size: int = 50_000) -> dict:
    """One fat scalar leaf aliased *reps* times.

    Node count is only reps+const, but json.dumps re-emits the scalar per
    reference, so the serialised output is reps*size bytes (~200MB here) --
    a byte-amplification OOM the node cap never sees. The byte cap catches it.
    """
    big = "A" * size
    return {"post-install": [{"type": "command", "run": [big] * reps}]}


def _deep_linear_chain(depth: int = 5000) -> dict:
    """A deep nest of DISTINCT single-child containers.

    Each container is referenced exactly once (no alias), so a container-id
    guard alone would pass it -- but json.dumps recurses one C frame per level
    and raises RecursionError past ~1000. The depth cap rejects it first.
    """
    node: object = "leaf"
    for _ in range(depth):
        node = [node]
    return {"post-install": [{"type": "command", "run": node}]}


@pytest.mark.parametrize(
    "builder",
    [_high_fanout_self_alias, _fat_scalar_aliased, _deep_linear_chain],
    ids=["high_fanout_self_alias", "fat_scalar_aliased", "deep_linear_chain"],
)
def test_round3_fingerprint_vectors_fail_closed_fast(builder):
    """Each round-3 abuse shape must fail closed (None) within a wall budget.

    Run the fingerprint on a worker thread so a regression that actually OOMs
    or hangs surfaces as a timeout assertion rather than killing the box.
    """
    import threading

    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    payload = builder()["post-install"][0]["run"]
    subtree = {"post-install": [{"type": "command", "run": payload}]}
    box: dict[str, object] = {}

    def _run():
        box["result"] = fingerprint_lifecycle_subtree(subtree)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "fingerprint did not bail fast -- guard not bounding the abuse"
    assert box.get("result") is None


def test_deep_chain_rejected_by_depth_cap():
    from apm_cli.core.script_trust import _is_fingerprint_safe

    assert _is_fingerprint_safe(_deep_linear_chain()) is False


def test_fat_scalar_rejected_by_byte_cap():
    from apm_cli.core.script_trust import _is_fingerprint_safe

    assert _is_fingerprint_safe(_fat_scalar_aliased()) is False


def _fat_int_aliased(reps: int = 90_000, digits: int = 4000) -> dict:
    """One fat INTEGER scalar leaf aliased *reps* times.

    PyYAML safe_load decodes an unbounded integer scalar to a Python int, and
    json.dumps emits its FULL decimal expansion per reference. The node count
    stays small (reps + const) and a magnitude-blind byte estimate (every int
    == a few bytes) under-counts the output, so both caps pass and json.dumps
    then materialises hundreds of MB. The byte cost MUST be magnitude-aware so
    the cumulative byte cap fires before json.dumps allocates.
    """
    big = int("9" * digits)
    return {"post-install": [{"type": "command", "run": [big] * reps}]}


def test_fat_int_rejected_by_byte_cap():
    """A giant integer aliased many times must fail closed via the byte cap.

    Regression trap for r4-trust-1: _leaf_byte_cost modelled every int as 8
    bytes, so a 4000-digit int aliased 90k times slipped under both caps and
    let json.dumps allocate ~360MB pre-trust. The fix makes int cost scale
    with bit_length so the 1MB byte cap rejects it.
    """
    import threading

    from apm_cli.core.script_trust import _is_fingerprint_safe, fingerprint_lifecycle_subtree

    subtree = _fat_int_aliased()
    assert _is_fingerprint_safe(subtree) is False

    box: dict[str, object] = {}

    def _run():
        box["result"] = fingerprint_lifecycle_subtree(subtree)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), (
        "big-int fingerprint did not bail fast -- byte cap not magnitude-aware"
    )
    assert box.get("result") is None


def test_benign_small_int_still_accepted():
    """A legit manifest with small integer fields (timeoutSec) must still pass."""
    from apm_cli.core.script_trust import _is_fingerprint_safe, fingerprint_lifecycle_subtree

    manifest = {"post-install": [{"type": "command", "run": "echo hi", "timeoutSec": 30}]}
    assert _is_fingerprint_safe(manifest) is True
    assert fingerprint_lifecycle_subtree(manifest) is not None


def test_realistic_manifest_not_false_rejected():
    """A legit manifest with many entries reusing interned scalars must pass.

    CPython interns short strings/small ints, so ``type: command`` repeats an
    id across every entry. The guard dedups only CONTAINER ids, never scalars,
    so a normal multi-event manifest must still fingerprint (no false reject).
    """
    from apm_cli.core.script_trust import _is_fingerprint_safe, fingerprint_lifecycle_subtree

    manifest = {
        event: [
            {"type": "command", "run": f"echo {event} {i}", "timeoutSec": 30} for i in range(20)
        ]
        for event in ("post-install", "pre-install", "post-update", "pre-run")
    }
    assert _is_fingerprint_safe(manifest) is True
    assert fingerprint_lifecycle_subtree(manifest) is not None


# ---------------------------------------------------------------------------
# VECTOR 2 (SECURE): trust identity / symlink swap / path normalization
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlink_swap_fails_closed(monkeypatch, tmp_path):
    """Trust /repo/apm.yml (a symlink), then repoint it -> must NOT stay trusted."""
    _isolate_apm_home(monkeypatch, tmp_path)
    good = tmp_path / "good"
    evil = tmp_path / "evil"
    good.mkdir()
    evil.mkdir()
    _write_apm_yml(good, "lifecycle:\n  post-install:\n    - {type: command, run: echo good}\n")
    _write_apm_yml(evil, "lifecycle:\n  post-install:\n    - {type: command, run: echo evil}\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "apm.yml"
    link.symlink_to(good / "apm.yml")

    from apm_cli.core.script_trust import (
        is_project_scripts_trusted,
        trust_project_scripts,
    )

    trust_project_scripts(link)
    assert is_project_scripts_trusted(link) is True

    link.unlink()
    link.symlink_to(evil / "apm.yml")
    # The resolved path changed -> stored key no longer matches -> untrusted.
    assert is_project_scripts_trusted(link) is False


def test_path_normalization_is_consistent(monkeypatch, tmp_path):
    """trust('./apm.yml') and check('apm.yml') resolve to the same key."""
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    _write_apm_yml(proj, "lifecycle:\n  post-install:\n    - {type: command, run: echo hi}\n")

    from apm_cli.core.script_trust import (
        is_fingerprint_trusted,
        script_file_fingerprint,
        trust_project_scripts,
    )

    dotted = proj / "." / "apm.yml"
    trust_project_scripts(dotted)
    plain = proj / "apm.yml"
    fp = script_file_fingerprint(plain)
    assert is_fingerprint_trusted(plain, fp) is True


# ---------------------------------------------------------------------------
# VECTOR 3 (SECURE): atomic write + lock release
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_turd_and_mode_0600(monkeypatch, tmp_path):
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    yml = _write_apm_yml(proj, "lifecycle:\n  post-install:\n    - {type: command, run: echo hi}\n")

    from apm_cli.core.script_trust import _trust_store_path, trust_project_scripts

    trust_project_scripts(yml)
    store = _trust_store_path()
    assert store.is_file()
    # No leftover temp files in the store dir.
    turds = [p for p in store.parent.iterdir() if p.name.startswith(".scripts-trust.")]
    assert turds == []
    if os.name != "nt":
        assert (store.stat().st_mode & 0o777) == 0o600


def test_lock_released_on_exception(monkeypatch, tmp_path):
    """_trust_store_lock must release even if the body raises."""
    _isolate_apm_home(monkeypatch, tmp_path)
    from apm_cli.core import script_trust

    with pytest.raises(RuntimeError):
        with script_trust._trust_store_lock():
            raise RuntimeError("boom")

    # Lock is free again: a second acquisition completes without hanging.
    with script_trust._trust_store_lock():
        pass


# ---------------------------------------------------------------------------
# VECTOR 5 (SECURE): trust-store poisoning -> fail closed
# ---------------------------------------------------------------------------


def test_poison_non_json_store_fails_closed(monkeypatch, tmp_path):
    home = _isolate_apm_home(monkeypatch, tmp_path)
    (home / "scripts-trust.json").write_text("}{ not json \x00", encoding="utf-8")
    from apm_cli.core.script_trust import _load_trust_store

    assert _load_trust_store() == {}


def test_poison_wrong_shape_store_fails_closed(monkeypatch, tmp_path):
    home = _isolate_apm_home(monkeypatch, tmp_path)
    (home / "scripts-trust.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    from apm_cli.core.script_trust import _load_trust_store

    assert _load_trust_store() == {}


def test_poison_directory_store_fails_closed(monkeypatch, tmp_path):
    home = _isolate_apm_home(monkeypatch, tmp_path)
    (home / "scripts-trust.json").mkdir()
    from apm_cli.core.script_trust import _load_trust_store

    assert _load_trust_store() == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX /dev/null")
def test_poison_devnull_symlink_store_fails_closed(monkeypatch, tmp_path):
    home = _isolate_apm_home(monkeypatch, tmp_path)
    (home / "scripts-trust.json").symlink_to("/dev/null")
    from apm_cli.core.script_trust import _load_trust_store

    assert _load_trust_store() == {}


# ---------------------------------------------------------------------------
# VECTOR 4 (SECURE): real-process concurrency, no lost update, no torn read
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_multiprocess_trust_no_lost_update(monkeypatch, tmp_path):
    """K forked processes trust K distinct paths concurrently; all survive."""
    home = _isolate_apm_home(monkeypatch, tmp_path)
    os.environ["APM_HOME"] = str(home)  # ensure children inherit

    k = 16
    ymls = []
    for i in range(k):
        d = tmp_path / f"repo{i}"
        d.mkdir()
        ymls.append(
            _write_apm_yml(
                d,
                f"lifecycle:\n  post-install:\n    - {{type: command, run: echo {i}}}\n",
            )
        )

    from apm_cli.core.script_trust import _load_trust_store, trust_project_scripts

    pids = []
    for yml in ymls:
        pid = os.fork()
        if pid == 0:
            # child
            try:
                for _ in range(20):
                    trust_project_scripts(yml)
                os._exit(0)
            except BaseException:
                os._exit(1)
        pids.append(pid)

    for pid in pids:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

    store = _load_trust_store()
    expected = {str(y.resolve()) for y in ymls}
    assert expected.issubset(set(store)), f"lost update: {expected - set(store)}"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_concurrent_reader_never_sees_torn_json(monkeypatch, tmp_path):
    """A reader hammering the raw store file never observes a half-written file."""
    home = _isolate_apm_home(monkeypatch, tmp_path)
    os.environ["APM_HOME"] = str(home)
    store = home / "scripts-trust.json"

    proj = tmp_path / "repo"
    proj.mkdir()
    yml = _write_apm_yml(proj, "lifecycle:\n  post-install:\n    - {type: command, run: echo hi}\n")

    from apm_cli.core.script_trust import (
        trust_project_scripts,
        untrust_project_scripts,
    )

    # Seed so the file exists.
    trust_project_scripts(yml)

    writer = os.fork()
    if writer == 0:
        try:
            for _ in range(200):
                trust_project_scripts(yml)
                untrust_project_scripts(yml)
            os._exit(0)
        except BaseException:
            os._exit(1)

    torn = 0
    reads = 0
    for _ in range(400):
        try:
            raw = store.read_text(encoding="utf-8")
        except OSError:
            continue
        if raw == "":
            continue
        reads += 1
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            torn += 1

    _, status = os.waitpid(writer, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert torn == 0, f"observed {torn} torn reads out of {reads}"


# ---------------------------------------------------------------------------
# VECTOR 6 (SECURE): org deny_all ceiling suppresses even trusted projects
# ---------------------------------------------------------------------------


def test_deny_all_suppresses_even_trusted_project(monkeypatch, tmp_path):
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    yml = _write_apm_yml(proj, "lifecycle:\n  post-install:\n    - {type: command, run: echo hi}\n")

    from apm_cli.core.script_trust import trust_project_scripts

    trust_project_scripts(yml)

    class _Exe:
        deny_all = True

    class _Policy:
        executables = _Exe()

    class _Result:
        policy = _Policy()

    import apm_cli.policy.discovery as disc

    monkeypatch.setattr(disc, "discover_policy_with_chain", lambda *a, **k: _Result())

    from apm_cli.core.lifecycle_scripts import build_runner_from_context

    runner = build_runner_from_context(project_root=str(proj))
    assert runner.scripts_for_event("post-install") == []


def test_gate_drops_untrusted_keeps_after_trust(monkeypatch, tmp_path):
    """Baseline: untrusted project scripts dropped; trusting keeps them."""
    _isolate_apm_home(monkeypatch, tmp_path)
    proj = tmp_path / "repo"
    proj.mkdir()
    yml = _write_apm_yml(proj, "lifecycle:\n  post-install:\n    - {type: command, run: echo hi}\n")

    from apm_cli.core.lifecycle_scripts import build_runner_from_context
    from apm_cli.core.script_trust import trust_project_scripts

    runner = build_runner_from_context(project_root=str(proj))
    assert runner.scripts_for_event("post-install") == []

    trust_project_scripts(yml)
    runner2 = build_runner_from_context(project_root=str(proj))
    assert len(runner2.scripts_for_event("post-install")) == 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
