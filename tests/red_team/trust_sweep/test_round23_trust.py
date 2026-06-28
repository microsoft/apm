"""Round-23 red-team probes of the lifecycle-script TRUST surface.

Hunt: a GENUINE trust break the previous 22 rounds missed -- a BYPASS
(untrusted/mutated content fires as trusted), an OVER-TRUST (two distinct
executing subtrees share one fingerprint), a FAIL-OPEN (an unfingerprintable
subtree still fires), or a DoS (a legit path permanently un-trustable). The
decisive secure contract probed here:

    fingerprint(executed_content_A) != fingerprint(executed_content_B)
    for any A, B that EXECUTE differently, AND
    an un-fingerprintable subtree (fp None) never fires.

Round-23 pivots (chosen to avoid re-treading r21/r22 beyond confirming the
trap still holds on this head 087e1425a):

  A. HASH-vs-EXEC drift via an alias whose TARGET lives OUTSIDE the
     ``lifecycle:`` subtree -- (1) the whole ``lifecycle:`` value is an alias
     to an out-of-subtree node, (2) a single command scalar is an alias to an
     out-of-subtree scalar, (3) a ``<<`` merge pulls an out-of-subtree mapping
     into a lifecycle entry. Mutating the out-of-subtree anchor MUST re-gate
     (the RESOLVED value is baked into the subtree-only hash).
  B. CANONICALISATION collision: json.dumps(sort_keys=True) coerces non-string
     keys. A mixed-type-key dict must fail CLOSED (fp None), and a pure
     non-string-key dict must yield NO executable entry -- so key coercion can
     never forge a second lifecycle event under a colliding fingerprint.
  C. FAIL-CLOSED on an unfingerprintable (over-cap) but otherwise legit
     subtree: fp None -> cannot be trusted -> every project script is skipped
     end-to-end through build_runner_from_context (no fail-open firing).
  D. End-to-end gate through build_runner_from_context: untrusted never fires,
     trust enables, and editing ANY executed byte re-arms the gate.

Detect hangs with a daemon thread + join(timeout); the runtime bans the
``timeout`` shell command.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import (
    _entries_from_lifecycle_map,
    build_runner_from_context,
    parse_apm_yml_lifecycle_with_fingerprint,
)
from apm_cli.core.script_trust import (
    fingerprint_lifecycle_subtree,
    trust_project_scripts,
    untrust_project_scripts,
)

_ROOT = Path(tempfile.mkdtemp(prefix="rt23-trust-"))


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    yield Path(home)


def _write(text: str) -> Path:
    proj = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    yml = proj / "apm.yml"
    yml.write_text(text, encoding="utf-8")
    return yml


def _run_with_timeout(fn, seconds: float = 10.0):
    box: dict[str, object] = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "operation hung (possible parse/serialize DoS)"
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box["r"]


# -- A. alias target OUTSIDE the lifecycle subtree must be baked into the hash --


def test_lifecycle_value_is_alias_to_out_of_subtree_node_rebakes():
    """``lifecycle: *reg`` where &reg lives in another key -> resolved value hashed."""
    safe = _write("registries: &reg\n  post-install:\n    - run: echo SAFE\nlifecycle: *reg\n")
    evil = _write("registries: &reg\n  post-install:\n    - run: echo EVIL\nlifecycle: *reg\n")
    e_safe, f_safe = parse_apm_yml_lifecycle_with_fingerprint(safe, "project")
    e_evil, f_evil = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")

    assert [s.effective_command for s in e_safe] == ["echo SAFE"]
    assert [s.effective_command for s in e_evil] == ["echo EVIL"]
    # Over-trust would be f_safe == f_evil (trust SAFE -> EVIL fires). Must differ.
    assert f_safe is not None and f_evil is not None
    assert f_safe != f_evil


def test_command_scalar_alias_to_out_of_subtree_scalar_rebakes():
    """A command that is an alias to a scalar defined outside lifecycle re-gates."""
    safe = _write("vars:\n  cmd: &c echo SAFE\nlifecycle:\n  post-install:\n    - run: *c\n")
    evil = _write("vars:\n  cmd: &c echo EVIL\nlifecycle:\n  post-install:\n    - run: *c\n")
    e_safe, f_safe = parse_apm_yml_lifecycle_with_fingerprint(safe, "project")
    e_evil, f_evil = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")
    assert [s.effective_command for s in e_safe] == ["echo SAFE"]
    assert [s.effective_command for s in e_evil] == ["echo EVIL"]
    assert f_safe != f_evil


def test_merge_key_from_out_of_subtree_mapping_rebakes():
    """``<<: *base`` pulling an out-of-subtree mapping into an entry re-gates."""
    safe = _write("base: &b\n  run: echo SAFE\nlifecycle:\n  post-install:\n    - <<: *b\n")
    evil = _write("base: &b\n  run: echo EVIL\nlifecycle:\n  post-install:\n    - <<: *b\n")
    e_safe, f_safe = parse_apm_yml_lifecycle_with_fingerprint(safe, "project")
    e_evil, f_evil = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")
    assert [s.effective_command for s in e_safe] == ["echo SAFE"]
    assert [s.effective_command for s in e_evil] == ["echo EVIL"]
    assert f_safe != f_evil


# -- B. canonicalisation key-coercion can neither collide-with-exec nor forge --


def test_mixed_type_keys_fail_closed():
    """A dict mixing a bool key and the colliding str key cannot be sorted by
    json.dumps -> TypeError -> fingerprint None (fail closed), never a hash."""
    subtree = {"post-install": [{True: "x", "true": "x", "run": "echo X"}]}
    assert _run_with_timeout(lambda: fingerprint_lifecycle_subtree(subtree)) is None


def test_nonstring_event_key_yields_no_executable_entry():
    """An int/None key coerces to a JSON string but is not a lifecycle event,
    so key coercion can never forge a second firing event under one fp."""
    a = {"post-install": [{"run": "echo SAFE"}], 1: [{"run": "echo EVIL"}]}
    b = {"post-install": [{"run": "echo SAFE"}], "1": [{"run": "echo EVIL"}]}
    proj = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    ea = _entries_from_lifecycle_map(a, proj / "apm.yml", "project")
    eb = _entries_from_lifecycle_map(b, proj / "apm.yml", "project")
    # The int/str "1" key is not a recognised event: NEITHER fires EVIL.
    assert [s.effective_command for s in ea] == ["echo SAFE"]
    assert [s.effective_command for s in eb] == ["echo SAFE"]


def test_int_float_value_in_executed_field_rebakes():
    """30 vs 30.0 in an executed timeout serialise differently -> distinct fp."""
    a = {"post-install": [{"run": "echo X", "timeoutSec": 30}]}
    b = {"post-install": [{"run": "echo X", "timeoutSec": 30.0}]}
    assert fingerprint_lifecycle_subtree(a) != fingerprint_lifecycle_subtree(b)


# -- C. unfingerprintable (over-cap) legit subtree must FAIL CLOSED, not open --


def test_over_cap_subtree_fails_closed_end_to_end(tmp_apm_home):
    """A legit-but-over-node-cap lifecycle subtree -> fp None -> cannot be
    trusted -> every project script is skipped (no fail-open firing)."""
    lines = ["lifecycle:", "  post-install:"]
    lines.extend(f"    - run: echo {i}" for i in range(60_000))
    yml = _write("\n".join(lines) + "\n")
    entries, fp = parse_apm_yml_lifecycle_with_fingerprint(yml, "project")
    assert len(entries) == 60_000  # entries parse fine
    assert fp is None  # but the subtree is un-fingerprintable
    # Attempting to trust records nothing.
    assert trust_project_scripts(yml) is None
    runner = build_runner_from_context(project_root=str(yml.parent))
    assert len(runner._scripts) == 0
    assert runner._skipped_project_scripts == 60_000


# -- D. end-to-end gate: untrusted never fires; trust enables; edit re-arms --


def test_end_to_end_gate_untrusted_then_trust_then_edit(tmp_apm_home):
    yml = _write("lifecycle:\n  post-install:\n    - run: echo SAFE\n")
    root = str(yml.parent)

    # 1. Untrusted clone: nothing fires.
    r0 = build_runner_from_context(project_root=root)
    assert len(r0._scripts) == 0
    assert r0._skipped_project_scripts == 1

    # 2. Developer trusts the exact subtree: it now fires.
    assert trust_project_scripts(yml) is not None
    r1 = build_runner_from_context(project_root=root)
    assert [s.effective_command for s in r1._scripts] == ["echo SAFE"]

    # 3. Attacker edits the executed command: trust is re-armed (fingerprint
    #    revoked) -> the mutated command does NOT fire under stale trust.
    yml.write_text(
        "lifecycle:\n  post-install:\n    - run: echo EVIL\n",
        encoding="utf-8",
    )
    r2 = build_runner_from_context(project_root=root)
    assert len(r2._scripts) == 0
    assert r2._skipped_project_scripts == 1

    # 4. Editing a NON-lifecycle key must NOT revoke trust (usability invariant).
    yml.write_text(
        "lifecycle:\n  post-install:\n    - run: echo SAFE\ndependencies:\n  - some/pkg\n",
        encoding="utf-8",
    )
    r3 = build_runner_from_context(project_root=root)
    assert [s.effective_command for s in r3._scripts] == ["echo SAFE"]

    untrust_project_scripts(yml)


def test_unicode_nfc_nfd_command_content_rebakes():
    """NFC vs NFD encodings of the same glyph in a command are distinct bytes
    in the canonical JSON -> distinct fp (no normalisation over-trust)."""
    nfc = "echo caf\u00e9"  # e-acute as one codepoint
    nfd = "echo cafe\u0301"  # e + combining acute
    a = {"post-install": [{"run": nfc}]}
    b = {"post-install": [{"run": nfd}]}
    assert fingerprint_lifecycle_subtree(a) != fingerprint_lifecycle_subtree(b)
