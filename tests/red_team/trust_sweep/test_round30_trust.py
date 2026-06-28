"""Round-30 red-team probes of the lifecycle-script TRUST surface.

Twenty-first-round hunt (trust has been CLEAN r9-r29). All probes exercise the
REAL firing gate ``build_runner_from_context`` (the same path
``apm install`` / ``update`` / ``uninstall`` use), never a re-implementation,
and assert the SECURE contract end-to-end with an ``execute_script`` /
``dispatch_http_batch`` spy:

  * UNTRUSTED project apm.yml  -> the spy is NEVER called (no fire).
  * TRUSTED project apm.yml    -> the spy IS called (fires).
  * Transient / exotic error   -> install proceeds WITHOUT firing
                                  (fail-safe), never an uncaught abort.

Vectors probed this round (the priming's bypass-class pivot):

  1. macOS APFS case-insensitivity / path-key case drift -> can trusting
     ``/A/apm.yml`` over-trust a DIFFERENT-content file reachable via a
     case-variant path? (over-trust attempt)
  2. symlink read-swap between the single parse and the resolve() trust-key:
     does a swap after the in-memory parse but before the store lookup ever
     run untrusted content under a trusted fingerprint? (toctou attempt)
  3. json.dumps canonicalization collision: top-level numeric event-name key
     coercion (``1`` -> ``"1"``) and the env non-string-key coercion -- does
     ANY collision carry an EXECUTION differential? (collision attempt)
  4. org executables.deny_all ceiling: suppresses even a TRUSTED project on
     the real install gate; and a discovery error must not silently fire an
     UNTRUSTED project. (ceiling)
  5. multiprocess stampede: N concurrent installs of an UNTRUSTED repo --
     never a premature fire; ``apm install`` writes no trust. (toctou)
  6. user-tier vs project-tier precedence: a user-tier trusted entry must NOT
     make a project-tier (source=="project") script fire. (bypass)

A genuine BYPASS would show the spy FIRING now on an untrusted repo; a
fail-not-closed would show install ABORTING now. Hangs are caught with a
daemon thread + join (the runtime bans the ``timeout`` shell builtin / kill).
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import apm_cli.core.script_executors as _se
from apm_cli.core import script_trust
from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)

_ROOT = Path(tempfile.mkdtemp(prefix="rt30-trust-"))

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


def _fire_real_gate(project_root: str) -> list[str]:
    """Drive the REAL install gate and return the commands that FIRED.

    Spies both the synchronous command executor and the http batch dispatch
    so any fired script -- of either type -- is recorded. The spy replaces the
    executor by name on the module the runner imports it from, so the gate's
    own trust filtering is fully exercised before execution is even reached.
    """
    fired: list[str] = []

    def _spy_exec(script, event, **kw):
        fired.append(script.effective_command or script.url or "")

    def _spy_http(scripts, event, **kw):
        for s in scripts:
            fired.append(s.url or s.effective_command or "")
        return []

    def _go():
        with (
            mock.patch.object(_se, "execute_script", _spy_exec),
            mock.patch.object(_se, "dispatch_http_batch", _spy_http),
        ):
            runner = build_runner_from_context(project_root=project_root)
            runner.fire("pre-install", _event(project_root))

    _run_with_timeout(_go)
    return fired


def _run_with_timeout(fn, seconds: float = 25.0):
    box: dict[str, object] = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "gate hung (possible parse/serialize/resolve DoS)"
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box.get("r")


# --------------------------------------------------------------------------
# Sanity: the secure contract itself (red-before would invert these)
# --------------------------------------------------------------------------


def test_round30_untrusted_never_fires(tmp_apm_home):
    """SECURE: a freshly-cloned untrusted apm.yml fires NOTHING."""
    proj = _proj(_EVIL, "untrusted")
    assert _fire_real_gate(str(proj)) == []


def test_round30_trusted_fires(tmp_apm_home):
    """SECURE: explicit trust enables exactly the trusted command."""
    proj = _proj(_BENIGN, "trusted")
    assert script_trust.trust_project_scripts(proj / "apm.yml") is not None
    assert _fire_real_gate(str(proj)) == ["echo BENIGN"]


# --------------------------------------------------------------------------
# Vector 1 -- case-insensitive path-key drift: OVER-trust attempt
# --------------------------------------------------------------------------


def test_round30_case_variant_path_no_overtrust(tmp_apm_home):
    """Trusting one path must never trust a DIFFERENT-content file.

    On a case-insensitive FS a case-variant path refers to the SAME inode
    (same content), so it can only ever UNDER-trust (DoS), never over-trust.
    We trust a benign repo, then fire an unrelated evil repo whose resolved
    key differs: it must not fire. (Pure over-trust probe.)
    """
    benign = _proj(_BENIGN, "Case")
    assert script_trust.trust_project_scripts(benign / "apm.yml") is not None

    evil = _proj(_EVIL, "case")
    # Distinct directory, distinct resolved key -> must stay untrusted.
    assert _fire_real_gate(str(evil)) == []


def test_round30_case_variant_same_dir_no_overtrust(tmp_apm_home):
    """A case-variant of a trusted dir must not fire evil content.

    Build a path that differs only in case from the trusted dir. If the FS is
    case-insensitive the bytes ARE the benign bytes (no over-trust possible);
    if case-sensitive the path simply does not exist -> empty tier -> no fire.
    Either way the secure contract holds: evil bytes never ride benign trust.
    """
    benign = _proj(_BENIGN, "MixedCase")
    assert script_trust.trust_project_scripts(benign / "apm.yml") is not None
    swapped_case = benign.name.swapcase()
    variant = benign.parent / swapped_case
    fired = _fire_real_gate(str(variant))
    # Never PWNED; at most the benign command if the FS folds case.
    assert "echo PWNED" not in fired
    assert fired in ([], ["echo BENIGN"])


# --------------------------------------------------------------------------
# Vector 2 -- symlink read-swap between parse and resolve(): TOCTOU attempt
# --------------------------------------------------------------------------


def test_round30_symlink_target_swap_failsafe(tmp_apm_home):
    """A symlinked apm.yml repointed benign->evil must revoke trust.

    Trust the symlink while it targets benign content (its resolve() key is
    the benign target). Repoint it at evil content. At the next install the
    single parse reads EVIL (fingerprint = evil), the resolve() key now points
    at the evil target which is NOT in the store -> untrusted -> no fire. No
    window runs evil bytes under the benign fingerprint.
    """
    benign_file = _proj(_BENIGN, "lt-benign") / "apm.yml"
    evil_file = _proj(_EVIL, "lt-evil") / "apm.yml"

    link_dir = Path(tempfile.mkdtemp(dir=str(_ROOT), prefix="lt-link-"))
    link = link_dir / "apm.yml"
    try:
        link.symlink_to(benign_file)
    except OSError:
        pytest.skip("symlinks unavailable on this FS")

    assert script_trust.trust_project_scripts(link) is not None
    # Benign content fires while the link still points at benign.
    assert _fire_real_gate(str(link_dir)) == ["echo BENIGN"]

    # Repoint the SAME trusted link path at evil content.
    link.unlink()
    link.symlink_to(evil_file)
    assert _fire_real_gate(str(link_dir)) == []  # evil must NOT ride trust


def test_round30_content_swap_same_path_revokes(tmp_apm_home):
    """Overwriting a trusted apm.yml in place revokes trust (fingerprint shift)."""
    proj = _proj(_BENIGN, "swap")
    assert script_trust.trust_project_scripts(proj / "apm.yml") is not None
    assert _fire_real_gate(str(proj)) == ["echo BENIGN"]
    (proj / "apm.yml").write_text(_EVIL, encoding="utf-8")
    assert _fire_real_gate(str(proj)) == []


# --------------------------------------------------------------------------
# Vector 3 -- canonicalization collision: EXECUTION-differential attempt
# --------------------------------------------------------------------------


def test_round30_numeric_event_key_failsafe(tmp_apm_home):
    """A numeric top-level event key makes the whole manifest fail-closed.

    A lifecycle map mixing an int key (``1``) with string event keys cannot be
    canonicalised: json.dumps(sort_keys=True) raises TypeError comparing int vs
    str keys, so ``fingerprint_lifecycle_subtree`` returns None. The fail-safe
    contract then holds two ways: (a) ``trust`` records NOTHING (returns None),
    and (b) even if a stale store entry existed, project_fp is None so
    is_fingerprint_trusted returns False -> the gate fires nothing. An
    un-fingerprintable manifest is never auto-trusted.
    """
    numeric = "lifecycle:\n  1:\n    - run: echo NUMERIC\n  pre-install:\n    - run: echo BENIGN\n"
    proj = _proj(numeric, "numkey")
    # Un-fingerprintable -> trust is a no-op (fail-closed), nothing recorded.
    assert script_trust.script_file_fingerprint(proj / "apm.yml") is None
    assert script_trust.trust_project_scripts(proj / "apm.yml") is None
    # And the real gate fires nothing for the un-fingerprintable manifest.
    assert _fire_real_gate(str(proj)) == []

    # Defence-in-depth: even a planted store entry cannot enable it, because
    # the current fingerprint is None (compared != any stored hex).
    store = script_trust._trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    key = str((proj / "apm.yml").resolve())
    store.write_text(f'{{"version": 1, "projects": {{"{key}": "deadbeef"}}}}', encoding="utf-8")
    assert _fire_real_gate(str(proj)) == []


def test_round30_env_key_coercion_no_command_differential(tmp_apm_home):
    """An int env key collides on fingerprint but yields no command differential.

    ``env: {1: V}`` and ``env: {"1": V}`` share one fingerprint (key coercion),
    but the FIRED command bytes are byte-identical, and an int env key fails
    closed at subprocess.Popen. Trusting one variant therefore cannot ride a
    DIFFERENT executed payload -- the command string is the same either way.
    """
    base = "lifecycle:\n  pre-install:\n    - run: echo SAME\n      env:\n        {key}: V\n"
    str_variant = _proj(base.format(key='"1"'), "envstr")
    int_variant = _proj(base.format(key="1"), "envint")

    fp_str = script_trust.script_file_fingerprint(str_variant / "apm.yml")
    fp_int = script_trust.script_file_fingerprint(int_variant / "apm.yml")
    # Collision is real at the fingerprint level...
    assert fp_str is not None and fp_str == fp_int
    # ...but trusting one does NOT change the executed command of the other:
    assert script_trust.trust_project_scripts(str_variant / "apm.yml") is not None
    assert _fire_real_gate(str(int_variant)) == []  # different resolved key
    # The str variant fires only its own (identical) command.
    assert _fire_real_gate(str(str_variant)) == ["echo SAME"]


# --------------------------------------------------------------------------
# Vector 4 -- org executables.deny_all ceiling
# --------------------------------------------------------------------------


def _deny_all_result(deny: bool):
    policy = SimpleNamespace(executables=SimpleNamespace(deny_all=deny))
    return SimpleNamespace(policy=policy)


def test_round30_deny_all_suppresses_trusted(tmp_apm_home):
    """The org deny_all ceiling suppresses even a TRUSTED project on install."""
    proj = _proj(_BENIGN, "denytrust")
    assert script_trust.trust_project_scripts(proj / "apm.yml") is not None
    # Sanity: trusted fires without the ceiling.
    assert _fire_real_gate(str(proj)) == ["echo BENIGN"]

    import apm_cli.policy.discovery as disc

    with mock.patch.object(
        disc, "discover_policy_with_chain", lambda *a, **k: _deny_all_result(True)
    ):
        assert _fire_real_gate(str(proj)) == []  # ceiling overrides trust


def test_round30_deny_all_discovery_error_keeps_untrusted_closed(tmp_apm_home):
    """A deny_all discovery error must not OPEN an untrusted project.

    The ceiling is best-effort (errors -> ceiling off), but the trust gate is
    independent: even if policy discovery throws, an untrusted project still
    fires nothing. (Fail-open of the ceiling never escalates an untrusted repo.)
    """
    proj = _proj(_EVIL, "denyerr")
    import apm_cli.policy.discovery as disc

    def _boom(*a, **k):
        raise RuntimeError("transient policy fetch failure")

    with mock.patch.object(disc, "discover_policy_with_chain", _boom):
        assert _fire_real_gate(str(proj)) == []


# --------------------------------------------------------------------------
# Vector 5 -- multiprocess stampede: no premature fire / lost-deny
# --------------------------------------------------------------------------


def test_round30_concurrent_untrusted_install_never_fires(tmp_apm_home):
    """N threads installing the SAME untrusted repo concurrently never fire.

    ``apm install`` writes no trust, so no interleaving can flip the gate. We
    use threads (the runtime bans process-kill primitives); the in-process
    trust store + gate are shared, exactly the contended surface.
    """
    proj = _proj(_EVIL, "stampede")
    results: list[list[str]] = []
    lock = threading.Lock()

    def _worker():
        out = _fire_real_gate(str(proj))
        with lock:
            results.append(out)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(25)
        assert not t.is_alive(), "concurrent gate hung"
    assert len(results) == 8
    assert all(r == [] for r in results), f"premature fire under contention: {results}"


# --------------------------------------------------------------------------
# Vector 6 -- user-tier trust must not fire a project-tier script
# --------------------------------------------------------------------------


def test_round30_user_tier_trust_does_not_fire_project(tmp_apm_home):
    """A trusted user-tier entry must not enable a project-tier script.

    Plant an identical lifecycle in the (ungated) user tier AND in an untrusted
    project. Trust only the user-tier path. The user-tier script fires (it is
    developer-controlled, ungated by design) but the project-tier script -- the
    one a clone ships -- must still be skipped. We assert no DUPLICATE fire and
    that the project entry's source is gated.
    """
    # User tier: $APM_HOME/apm.yml (ungated).
    user_yml = tmp_apm_home / "apm.yml"
    user_yml.write_text("lifecycle:\n  pre-install:\n    - run: echo USERTIER\n", encoding="utf-8")
    # Untrusted project ships a DISTINCT command.
    proj = _proj("lifecycle:\n  pre-install:\n    - run: echo PROJTIER\n", "usertier")

    # Trust the USER path explicitly (even though the user tier is ungated,
    # prove a user-tier trust record cannot leak onto the project tier).
    script_trust.trust_project_scripts(user_yml)

    fired = _fire_real_gate(str(proj))
    assert "echo PROJTIER" not in fired, "project-tier script fired ungated!"
    assert fired == ["echo USERTIER"]
