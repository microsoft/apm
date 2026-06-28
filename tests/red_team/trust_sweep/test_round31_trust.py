"""Round-31 red-team probes of the lifecycle-script TRUST surface.

Trust has been CLEAN r9-r30 (22 consecutive rounds -- the most robust
domain). This round pivots to genuinely NEW TOCTOU / canonicalization
vectors, all driven through the REAL firing gate
``build_runner_from_context`` (the same path ``apm install`` / ``update`` /
``uninstall`` use), never a re-implementation. Each probe asserts the
SECURE contract end-to-end with an ``execute_script`` / ``dispatch_http_batch``
spy:

  * UNTRUSTED project apm.yml  -> the spy is NEVER called (no fire).
  * TRUSTED project apm.yml    -> the spy IS called (fires).
  * Transient / exotic error   -> install proceeds WITHOUT firing
                                  (fail-safe), never an uncaught abort.

The over-trust hypothesis under test: can an UNTRUSTED project's
``execute_script`` / ``dispatch_http_batch`` fire WITHOUT the user having
trusted exactly those bytes? Under-trust (fail-safe DoS) is NOT a break.

Vectors probed this round:

  1.  Trust-write RACE: a concurrent ``trust_project_scripts`` write while the
      gate reads the store -- a reader must observe either the pre-write
      (no-trust) or post-write (correct-trust) state, never a torn file that
      trusts EVIL. (toctou / atomicity)
  2.  TOCTOU between the build decision and ``fire()``: swap apm.yml AFTER
      ``build_runner_from_context`` returns. The in-memory decision/entries
      are frozen; an untrusted build never fires even if the file becomes
      trusted-looking. (toctou)
  3.  Unicode NFC/NFD command collision: trusting the precomposed form must
      NOT trust the decomposed-byte variant (distinct command bytes). (collision)
  4.  Zero-width / RTL-override / trailing-whitespace / CRLF command coercion:
      each distinct byte-string must require its own trust. (collision)
  5.  Cross-project fingerprint reuse: two repos with BYTE-IDENTICAL lifecycle
      (same fingerprint) but different resolved paths -- trusting one must NOT
      trust the other (trust is PATH-keyed, not content-keyed). (bypass)
  6.  Trust-store duplicate project key (reader last-wins vs a planted WRONG
      fp): an evil fp planted as a duplicate must not over-trust. (format)
  7.  Trust-store project-key PREFIX: a trusted key that is a string prefix /
      superstring of the firing key must not match (exact dict lookup). (format)
  8.  Trust-store integer project VALUE: a non-string fp value is filtered ->
      never trusts. (format)
  9.  env VALUE coercion (int ``1`` vs str ``"1"``): a different value is a
      different fingerprint -> the int-value evil variant cannot ride the
      str-value trust. (collision)
  10. env/header KEY coercion (``1`` vs ``"1"``) is the ONLY canonicalization
      collision -- assert it is EXECUTION-EQUIVALENT (the command/URL bytes
      are identical) so it carries no differential. (collision, known-clean)
  11. Policy ceiling is one-directional: a benign/empty org policy never
      ENABLES an untrusted project; ``deny_all`` only SUPPRESSES. (ceiling)
  12. Concurrent 8-thread install of an untrusted repo: ``apm install`` writes
      NO trust, so no thread ever fires. (toctou)

A genuine BYPASS would show the spy FIRING now on an untrusted repo; a
fail-not-closed would show install ABORTING now. Hangs are caught with a
daemon thread + join (the runtime bans the ``timeout`` shell builtin / kill).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unicodedata
from pathlib import Path
from unittest import mock

import pytest

import apm_cli.core.script_executors as _se
from apm_cli.core import script_trust
from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)
from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

_ROOT = Path(tempfile.mkdtemp(prefix="rt31-trust-"))

_BENIGN = "lifecycle:\n  pre-install:\n    - run: echo BENIGN\n"
_EVIL = "lifecycle:\n  pre-install:\n    - run: echo PWNED\n"


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    yield Path(home)


def _proj(text: str, name: str | None = None) -> Path:
    d = Path(tempfile.mkdtemp(dir=str(_ROOT), prefix=(name or "p") + "-"))
    (d / "apm.yml").write_text(text, encoding="utf-8")
    return d


def _event(project_root: str) -> LifecycleEvent:
    return LifecycleEvent.create(
        event="pre-install",
        packages=[PackageInfo(name="x/y", reference="v0")],
        scope="project",
        working_directory=project_root,
    )


def _run_with_timeout(fn, seconds: float = 25.0):
    box: dict[str, object] = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:  # surface in caller thread
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "gate hung (possible parse/serialize/resolve DoS)"
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box.get("r")


def _build_and_fire(project_root: str, fired: list[str]) -> None:
    def _spy_exec(script, event, **kw):
        fired.append(script.effective_command or script.url or "")

    def _spy_http(scripts, event, **kw):
        for s in scripts:
            fired.append(s.url or s.effective_command or "")
        return []

    with (
        mock.patch.object(_se, "execute_script", _spy_exec),
        mock.patch.object(_se, "dispatch_http_batch", _spy_http),
    ):
        runner = build_runner_from_context(project_root=project_root)
        runner.fire("pre-install", _event(project_root))


def _fire_real_gate(project_root: str) -> list[str]:
    """Drive the REAL install gate and return the commands that FIRED.

    Spies both the synchronous command executor and the http batch dispatch on
    the module the runner imports them from, so the gate's own trust filtering
    runs in full before any executor is reached.
    """
    fired: list[str] = []
    _run_with_timeout(lambda: _build_and_fire(project_root, fired))
    return fired


# --------------------------------------------------------------------------
# Sanity: the secure contract itself (red-before would invert these)
# --------------------------------------------------------------------------


def test_round31_untrusted_never_fires(tmp_apm_home):
    """SECURE: a freshly-cloned untrusted apm.yml fires NOTHING."""
    proj = _proj(_EVIL, "untrusted")
    assert _fire_real_gate(str(proj)) == []


def test_round31_trusted_fires(tmp_apm_home):
    """SECURE: explicit trust enables exactly the trusted command."""
    proj = _proj(_BENIGN, "trusted")
    assert script_trust.trust_project_scripts(proj / "apm.yml") is not None
    assert _fire_real_gate(str(proj)) == ["echo BENIGN"]


# --------------------------------------------------------------------------
# Vector 1 -- trust-write RACE: torn / partial store never trusts EVIL
# --------------------------------------------------------------------------


def test_round31_concurrent_trust_write_never_tears(tmp_apm_home):
    """A concurrent trust write must never expose a torn store that trusts evil.

    One thread hammers ``trust_project_scripts`` on a BENIGN repo (each call is
    an atomic os.replace), while the main thread repeatedly drives the real
    gate on an UNRELATED EVIL repo. The evil repo is never trusted, so no
    interleaving of the writer's load->replace window can ever make it fire.
    """
    benign = _proj(_BENIGN, "race-benign")
    evil = _proj(_EVIL, "race-evil")

    stop = threading.Event()

    def _writer():
        while not stop.is_set():
            script_trust.trust_project_scripts(benign / "apm.yml")
            script_trust.untrust_project_scripts(benign / "apm.yml")

    w = threading.Thread(target=_writer, daemon=True)
    w.start()
    try:
        for _ in range(40):
            assert _fire_real_gate(str(evil)) == [], "evil repo fired during trust race"
    finally:
        stop.set()
        w.join(5.0)


def test_round31_torn_store_bytes_fail_closed(tmp_apm_home):
    """A literally half-written store JSON must parse fail-closed (no trust).

    Plant a truncated trust file (valid prefix, cut mid-token) at the real
    store path, then fire an evil repo. ``_load_trust_store`` must treat the
    malformed JSON as ``{}`` -> untrusted -> no fire.
    """
    evil = _proj(_EVIL, "torn-evil")
    store = script_trust._trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    # A real, complete trust record for evil -- but TRUNCATED mid-value.
    good = json.dumps({"version": 1, "projects": {str((evil / "apm.yml").resolve()): "deadbeef"}})
    store.write_text(good[: len(good) // 2], encoding="utf-8")
    assert _fire_real_gate(str(evil)) == []


# --------------------------------------------------------------------------
# Vector 2 -- TOCTOU between the build decision and fire()
# --------------------------------------------------------------------------


def test_round31_swap_after_build_before_fire(tmp_apm_home):
    """Swapping apm.yml after build() but before fire() cannot inject scripts.

    The runner is built from an UNTRUSTED evil repo (-> empty kept list). We
    then overwrite the file with a benign body AND trust THAT body, then fire.
    Because the decision and entries were frozen at build time from the
    untrusted parse, fire() runs nothing -- no late-bound re-read.
    """
    proj = _proj(_EVIL, "swap")
    fired: list[str] = []

    def _spy_exec(script, event, **kw):
        fired.append(script.effective_command or "")

    def _spy_http(scripts, event, **kw):
        return []

    def _go():
        with (
            mock.patch.object(_se, "execute_script", _spy_exec),
            mock.patch.object(_se, "dispatch_http_batch", _spy_http),
        ):
            runner = build_runner_from_context(project_root=str(proj))
            # Attacker swaps to benign AND grants trust between build and fire.
            (proj / "apm.yml").write_text(_BENIGN, encoding="utf-8")
            script_trust.trust_project_scripts(proj / "apm.yml")
            runner.fire("pre-install", _event(str(proj)))

    _run_with_timeout(_go)
    assert fired == []


# --------------------------------------------------------------------------
# Vector 3/4 -- Unicode / invisible-char / whitespace command collisions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trusted_cmd, evil_cmd, label",
    [
        ("echo \u00e9", unicodedata.normalize("NFD", "echo \u00e9"), "nfc-vs-nfd"),
        ("echo hi", "echo hi ", "trailing-space"),
        ("echo hi", "echo hi\n", "trailing-newline"),
        ("echo hi", "echo hi\u200b", "zero-width-space"),
        ("echo hi", "echo hi\u202e", "rtl-override"),
        ("echo hi", "echo\thi", "tab-vs-space"),
    ],
)
def test_round31_command_byte_variants_need_own_trust(tmp_apm_home, trusted_cmd, evil_cmd, label):
    """A byte-distinct command must not ride a look-alike's trust.

    Trust a repo whose command is ``trusted_cmd``; then fire a SEPARATE repo
    whose command is the visually/structurally similar ``evil_cmd``. The two
    canonicalize to different fingerprints, and trust is path-keyed anyway, so
    the evil variant must never fire.
    """
    # Fingerprints must differ (no canonical collision).
    assert fingerprint_lifecycle_subtree(
        {"pre-install": [{"run": trusted_cmd}]}
    ) != fingerprint_lifecycle_subtree({"pre-install": [{"run": evil_cmd}]}), label

    trusted = _proj(f"lifecycle:\n  pre-install:\n    - run: {json.dumps(trusted_cmd)}\n", "t")
    assert script_trust.trust_project_scripts(trusted / "apm.yml") is not None

    evil = _proj(f"lifecycle:\n  pre-install:\n    - run: {json.dumps(evil_cmd)}\n", "e")
    fired = _fire_real_gate(str(evil))
    assert fired == [], f"{label}: evil byte-variant fired on a look-alike's trust"


# --------------------------------------------------------------------------
# Vector 5 -- cross-project fingerprint reuse (trust is PATH-keyed)
# --------------------------------------------------------------------------


def test_round31_identical_fingerprint_other_path_no_overtrust(tmp_apm_home):
    """Byte-identical lifecycle in two repos: trusting one never trusts the other.

    Both repos have the SAME lifecycle bytes -> SAME fingerprint. Trust repo A.
    Repo B (same content, different resolved path) must stay untrusted: the
    store key is the resolved apm.yml path, not the content hash.
    """
    a = _proj(_BENIGN, "reuse-A")
    b = _proj(_BENIGN, "reuse-B")
    fp_a = script_trust.trust_project_scripts(a / "apm.yml")
    fp_b = fingerprint_lifecycle_subtree({"pre-install": [{"run": "echo BENIGN"}]})
    assert fp_a == fp_b  # identical content -> identical fingerprint
    # A fires (trusted); B with the SAME fingerprint but other path does NOT.
    assert _fire_real_gate(str(a)) == ["echo BENIGN"]
    assert _fire_real_gate(str(b)) == []


# --------------------------------------------------------------------------
# Vector 6/7/8 -- trust-store FORMAT edges
# --------------------------------------------------------------------------


def _plant_store(text: str) -> None:
    store = script_trust._trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(text, encoding="utf-8")


def test_round31_duplicate_project_key_last_wins_wrong_fp(tmp_apm_home):
    """A planted duplicate project key with a WRONG fp must not over-trust.

    The real content fingerprint is fp_real. We plant the project key TWICE:
    first with fp_real, then (last, which json.loads keeps) with a bogus fp.
    The reader's last-wins value != fp_real -> untrusted -> no fire. (And even
    if first-wins, a deliberately-wrong duplicate cannot help an attacker.)
    """
    evil = _proj(_EVIL, "dupkey")
    key = str((evil / "apm.yml").resolve())
    fp_real = fingerprint_lifecycle_subtree({"pre-install": [{"run": "echo PWNED"}]})
    # Hand-build raw JSON with a duplicate key (json.dumps can't emit dup keys).
    raw = (
        '{"version":1,"projects":{'
        f"{json.dumps(key)}:{json.dumps(fp_real)},"
        f"{json.dumps(key)}:{json.dumps('00bad00')}"
        "}}"
    )
    _plant_store(raw)
    assert _fire_real_gate(str(evil)) == []


def test_round31_key_prefix_no_match(tmp_apm_home):
    """A trusted key that is a string prefix of the firing key must not match."""
    evil = _proj(_EVIL, "prefix")
    key = str((evil / "apm.yml").resolve())
    fp_real = fingerprint_lifecycle_subtree({"pre-install": [{"run": "echo PWNED"}]})
    # Trust a key that is a strict prefix (drop the trailing 'l' of apm.yml).
    _plant_store(json.dumps({"version": 1, "projects": {key[:-1]: fp_real}}))
    assert _fire_real_gate(str(evil)) == []


def test_round31_integer_fp_value_filtered(tmp_apm_home):
    """A non-string fp value in the store is filtered -> never trusts."""
    evil = _proj(_EVIL, "intval")
    key = str((evil / "apm.yml").resolve())
    # Plant the correct path key but with an INTEGER value (not the str fp).
    _plant_store(json.dumps({"version": 1, "projects": {key: 12345}}))
    assert _fire_real_gate(str(evil)) == []
    # And the loader still returns a clean dict (no crash on mixed values).
    assert script_trust._load_trust_store() == {}


# --------------------------------------------------------------------------
# Vector 9 -- env VALUE coercion is a fingerprint DIFFERENTIAL (not collision)
# --------------------------------------------------------------------------


def test_round31_env_value_int_vs_str_distinct_fp(tmp_apm_home):
    """env value int 1 vs str '1' fingerprint differently -> no over-trust.

    Trust the str-value form; an evil repo using the int-value form has a
    different fingerprint and a different path, so it cannot fire.
    """
    str_form = {"pre-install": [{"run": "echo X", "env": {"A": "1"}}]}
    int_form = {"pre-install": [{"run": "echo X", "env": {"A": 1}}]}
    assert fingerprint_lifecycle_subtree(str_form) != fingerprint_lifecycle_subtree(int_form)

    trusted = _proj(
        "lifecycle:\n  pre-install:\n    - run: echo X\n      env:\n        A: '1'\n", "envstr"
    )
    assert script_trust.trust_project_scripts(trusted / "apm.yml") is not None
    evil = _proj(
        "lifecycle:\n  pre-install:\n    - run: echo X\n      env:\n        A: 1\n", "envint"
    )
    assert _fire_real_gate(str(evil)) == []


# --------------------------------------------------------------------------
# Vector 10 -- env/header KEY coercion: the ONLY collision, EXECUTION-EQUIVALENT
# --------------------------------------------------------------------------


def test_round31_env_key_coercion_is_execution_equivalent(tmp_apm_home):
    """env key int 1 and str '1' collide -- but the COMMAND bytes are identical.

    The fingerprint collapses the key type, but the executed command ("run")
    is a JSON VALUE, preserved exactly and identical across both forms, so the
    collision carries NO command differential. We trust the str-key form, then
    fire it: exactly the same command runs that the user saw and approved.
    """
    str_key = {"pre-install": [{"run": "echo SAME", "env": {"1": "v"}}]}
    int_key = {"pre-install": [{"run": "echo SAME", "env": {1: "v"}}]}
    assert fingerprint_lifecycle_subtree(str_key) == fingerprint_lifecycle_subtree(int_key)

    trusted = _proj(
        "lifecycle:\n  pre-install:\n    - run: echo SAME\n      env:\n        '1': v\n", "keystr"
    )
    assert script_trust.trust_project_scripts(trusted / "apm.yml") is not None
    # The trusted command is exactly what the collision-equal int-key form
    # would also run: 'echo SAME'. No differential.
    assert _fire_real_gate(str(trusted)) == ["echo SAME"]


# --------------------------------------------------------------------------
# Vector 11 -- policy ceiling is one-directional (never ENABLES untrusted)
# --------------------------------------------------------------------------


def test_round31_benign_policy_does_not_enable_untrusted(tmp_apm_home, monkeypatch):
    """A benign/empty org policy never makes an untrusted project fire.

    The gate reads only ``executables.deny_all`` (a suppress-only ceiling).
    We stub policy discovery to return a policy with deny_all False and assert
    the untrusted evil repo still fires nothing.
    """
    evil = _proj(_EVIL, "policy-benign")

    class _Execs:
        deny_all = False

    class _Pol:
        executables = _Execs()

    class _Res:
        policy = _Pol()

    import apm_cli.policy.discovery as _disc

    monkeypatch.setattr(_disc, "discover_policy_with_chain", lambda root: _Res(), raising=False)
    assert _fire_real_gate(str(evil)) == []


def test_round31_deny_all_suppresses_even_trusted(tmp_apm_home, monkeypatch):
    """deny_all suppresses even a TRUSTED project (ceiling holds downward)."""
    proj = _proj(_BENIGN, "policy-deny")
    assert script_trust.trust_project_scripts(proj / "apm.yml") is not None

    class _Execs:
        deny_all = True

    class _Pol:
        executables = _Execs()

    class _Res:
        policy = _Pol()

    import apm_cli.policy.discovery as _disc

    monkeypatch.setattr(_disc, "discover_policy_with_chain", lambda root: _Res(), raising=False)
    assert _fire_real_gate(str(proj)) == []


# --------------------------------------------------------------------------
# Vector 12 -- 8-thread concurrent install of an UNTRUSTED repo: no fire
# --------------------------------------------------------------------------


def test_round31_concurrent_untrusted_installs_never_fire(tmp_apm_home):
    """Eight concurrent gate drives on an untrusted repo fire nothing.

    ``apm install`` writes no trust, so no scheduling interleaving can promote
    the untrusted project to trusted mid-flight.
    """
    evil = _proj(_EVIL, "stampede")
    results: list[list[str]] = []
    lock = threading.Lock()

    def _one():
        fired: list[str] = []
        _build_and_fire(str(evil), fired)
        with lock:
            results.append(fired)

    threads = [threading.Thread(target=_one, daemon=True) for _ in range(8)]

    def _go():
        for t in threads:
            t.start()
        for t in threads:
            t.join(20.0)

    _run_with_timeout(_go, seconds=30.0)
    assert all(not t.is_alive() for t in threads), "a concurrent install hung"
    assert results and all(r == [] for r in results), "untrusted repo fired under concurrency"
