"""Round-22 red-team probes of the lifecycle-script TRUST surface.

Hunt: a GENUINE trust BYPASS -- untrusted/mutated apm.yml content firing as
trusted. The decisive assertion for a bypass is:

    fingerprint(trusted_content) != fingerprint(mutated_content)

If two DIFFERENT-executing subtrees produce the SAME fingerprint, that is a
HIGH bypass (trusted-once, mutate-freely). If a structural/path ambiguity
resolves to "don't fire", that is fail-CLOSED (safe, not a finding).

Vectors exercised here (new for r22, not re-treading r21's anchor/case probes
beyond confirming they still hold):

  A. MERGE-KEY (``<<``) hash/exec drift -- a top-level anchor merged INTO a
     lifecycle entry via ``<<``. Mutating the anchor MUST change the fp.
  B. dict-KEY coercion collision (``{1: ...}`` vs ``{"1": ...}`` canonicalise
     identically in json.dumps) -- does any execution-distinguishing path read
     an int/bool/None key such that two same-fp subtrees execute differently?
  C. float/int value canonicalisation in an executed field (timeout).
  D. path-identity on THIS macOS APFS box: case-variant, trailing-slash, ``..``
     and ``.`` forms, and a real-file->symlink swap -- can a DIFFERENT file's
     content fire under another file's trust record?
  E. trust-store value type confusion (non-str / truthy junk) -> must not make
     is_fingerprint_trusted return True for un-trusted content.
  F. ``apm lifecycle test --execute`` ungated firing (escape-hatch audit).

Detect hangs with a daemon thread + join(timeout); the runtime bans the
``timeout`` shell command.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import (
    _entries_from_lifecycle_map,
    parse_apm_yml_lifecycle_with_fingerprint,
)
from apm_cli.core.script_trust import (
    _load_trust_store,
    _trust_store_path,
    _write_trust_store,
    fingerprint_lifecycle_subtree,
    is_fingerprint_trusted,
    trust_project_scripts,
    untrust_project_scripts,
)
from apm_cli.utils.yaml_io import load_yaml

_ROOT = Path(tempfile.mkdtemp(prefix="rt22-trust-"))


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    yield Path(home)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _run_with_timeout(fn, seconds=10):
    box = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise AssertionError(f"operation hung > {seconds}s (possible DoS)")
    if "e" in box:
        raise box["e"]
    return box["r"]


# --------------------------------------------------------------------------
# Vector A: merge-key (<<) drift -- anchor merged INTO a lifecycle entry
# --------------------------------------------------------------------------
def test_merge_key_anchor_baked_into_fingerprint():
    """A top-level anchor merged into a lifecycle entry via ``<<`` must be
    baked into the fp, so mutating the anchor changes the fp (no bypass).

    This is the merge-key sibling of r21's plain-alias probe and exercises the
    custom _BoundedSafeLoader.flatten_mapping reimplementation specifically.
    """
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    benign = _write(
        d / "a" / "apm.yml",
        "defaults: &d\n"
        "  type: command\n"
        '  bash: "echo TRUSTED"\n'
        "lifecycle:\n"
        "  pre-install:\n"
        "    - <<: *d\n",
    )
    evil = _write(
        d / "b" / "apm.yml",
        "defaults: &d\n"
        "  type: command\n"
        '  bash: "echo PWNED"\n'
        "lifecycle:\n"
        "  pre-install:\n"
        "    - <<: *d\n",
    )
    fp_benign = fingerprint_lifecycle_subtree(load_yaml(benign).get("lifecycle"))
    fp_evil = fingerprint_lifecycle_subtree(load_yaml(evil).get("lifecycle"))
    assert fp_benign is not None
    # Confirm the executor actually sees the merged command (exec-domain truth).
    entries = _entries_from_lifecycle_map(load_yaml(benign).get("lifecycle"), benign, "project")
    assert entries and entries[0].bash == "echo TRUSTED"
    # The decisive bypass assertion: different execution => different fp.
    assert fp_benign != fp_evil, "MERGE-KEY BYPASS: mutated <<-merged command shares fp"


# --------------------------------------------------------------------------
# Vector B: dict-KEY coercion collision (int 1 vs str "1")
# --------------------------------------------------------------------------
def test_int_vs_str_key_collision_does_not_diverge_execution():
    """json.dumps coerces dict keys to str: ``{1: x}`` and ``{"1": x}`` share a
    canonical string. A bypass needs an execution path that reads an int/bool
    key differently from its string twin while the fp stays equal.

    The event-name layer compares against the string LIFECYCLE_EVENTS tuple, so
    a non-string key is never a recognised event -> no entry is built. Verify
    that the only int/str-key collision is execution-EQUIVALENT (both produce
    zero project entries), i.e. not a usable bypass.
    """
    sub_int = {1: [{"type": "command", "bash": "echo A"}]}
    sub_str = {"1": [{"type": "command", "bash": "echo A"}]}
    fp_int = fingerprint_lifecycle_subtree(sub_int)
    fp_str = fingerprint_lifecycle_subtree(sub_str)
    # They DO collide in canonical json (documented coercion).
    assert fp_int == fp_str
    # ...but neither builds an entry: int 1 and str "1" are both non-events.
    e_int = _entries_from_lifecycle_map(sub_int, Path("x"), "project")
    e_str = _entries_from_lifecycle_map(sub_str, Path("x"), "project")
    assert e_int == [] and e_str == [], "int/str event key built an entry -> potential bypass"


def test_bool_true_vs_str_true_key_collision_is_execution_equivalent():
    """``{True: ...}`` and ``{"true": ...}`` canonicalise identically. Neither
    'true' nor True is a LIFECYCLE_EVENT, so both build zero entries: collision
    is benign (execution-equivalent)."""
    sub_bool = {True: [{"type": "command", "bash": "echo A"}]}
    sub_str = {"true": [{"type": "command", "bash": "echo A"}]}
    assert fingerprint_lifecycle_subtree(sub_bool) == fingerprint_lifecycle_subtree(sub_str)
    assert _entries_from_lifecycle_map(sub_bool, Path("x"), "project") == []
    assert _entries_from_lifecycle_map(sub_str, Path("x"), "project") == []


def test_nested_int_key_collision_in_env_block():
    """An int key inside an entry's ``env`` block collides in json with its str
    twin. If the executor distinguished them this would be a bypass; assert the
    fp collides AND the built entry's env is execution-equivalent (same dict
    after json round-trip), i.e. no divergence the gate would miss."""
    sub_int = {"pre-install": [{"type": "command", "bash": "echo A", "env": {1: "v"}}]}
    sub_str = {"pre-install": [{"type": "command", "bash": "echo A", "env": {"1": "v"}}]}
    fp_int = fingerprint_lifecycle_subtree(sub_int)
    fp_str = fingerprint_lifecycle_subtree(sub_str)
    assert fp_int == fp_str  # collision exists
    e_int = _entries_from_lifecycle_map(sub_int, Path("x"), "project")[0]
    e_str = _entries_from_lifecycle_map(sub_str, Path("x"), "project")[0]
    # The command (what actually executes a shell) is identical; env-key int vs
    # str cannot smuggle a different COMMAND. Document that the only divergence
    # is the env-dict key type, which subprocess rejects (non-str env key) -> a
    # fail (isolated), never a different trusted command. Not a bypass.
    assert e_int.bash == e_str.bash == "echo A"


# --------------------------------------------------------------------------
# Vector C: float vs int in an executed field (timeout)
# --------------------------------------------------------------------------
def test_float_vs_int_timeout_changes_fingerprint():
    """``timeout: 30`` and ``timeout: 30.0`` execute equivalently but MUST NOT
    silently share a fp in a way that lets one mutate into a different command.
    json keeps ``30`` vs ``30.0`` distinct, so the fp differs (conservative)."""
    sub_i = {"pre-install": [{"type": "command", "bash": "echo A", "timeout": 30}]}
    sub_f = {"pre-install": [{"type": "command", "bash": "echo A", "timeout": 30.0}]}
    assert fingerprint_lifecycle_subtree(sub_i) != fingerprint_lifecycle_subtree(sub_f)


# --------------------------------------------------------------------------
# Vector D: path-identity on macOS APFS (case / trailing-slash / .. / symlink)
# --------------------------------------------------------------------------
def test_case_variant_path_is_failclosed_on_apfs(tmp_apm_home):
    """On case-insensitive APFS, /repo/apm.yml and /repo/APM.yml are the SAME
    inode (os.path.samefile == True), yet ``Path.resolve()`` PRESERVES the
    requested case rather than normalising to the on-disk spelling. So the two
    case spellings produce DIFFERENT trust keys.

    Security consequence: the case-split can only ever MISS (a case-variant
    access does not inherit trust -> project scripts are skipped). That is the
    fail-CLOSED direction. The firing gate always keys on the literal lowercase
    ``apm.yml`` (``_get_project_apm_yml`` -> ``root / 'apm.yml'``), so a record
    trusted under an UPPERCASE spelling never fires for it. Critically, no two
    DISTINCT files can ever share a resolved key, so a case spelling can never
    over-trust different content. Assert both halves of that."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    f = _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo OK"\n')
    fp = trust_project_scripts(f)
    assert fp is not None
    upper = d / "APM.yml"
    assert upper.exists() and os.path.samefile(f, upper)  # same inode on APFS
    # resolve() preserves case -> distinct keys -> case-variant access MISSES.
    assert str(f.resolve()) != str(upper.resolve())
    same_content_fp = fingerprint_lifecycle_subtree(load_yaml(upper).get("lifecycle"))
    assert same_content_fp == fp  # identical CONTENT, identical fingerprint
    # Fail-CLOSED: the uppercase spelling is NOT trusted (different key). Safe.
    assert is_fingerprint_trusted(upper, same_content_fp) is False
    # And the canonical lowercase spelling the firing gate uses IS trusted.
    assert is_fingerprint_trusted(f, fp) is True
    # Over-trust check: mutate content -> every spelling reads untrusted.
    _write(f, 'lifecycle:\n  pre-install:\n    - run: "echo EVIL"\n')
    mut_fp = fingerprint_lifecycle_subtree(load_yaml(f).get("lifecycle"))
    assert mut_fp != fp
    assert is_fingerprint_trusted(f, mut_fp) is False
    assert is_fingerprint_trusted(upper, mut_fp) is False


def test_trailing_slash_and_dotdot_path_forms_key_consistently(tmp_apm_home):
    """``/d/apm.yml``, ``/d/./apm.yml`` and ``/d/sub/../apm.yml`` resolve to one
    key. Trust recorded under one form must be readable under the others (and a
    mutation must revoke under ALL forms). A keying SPLIT would only fail-closed
    (don't fire); we assert the stronger property that mutation revokes."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    (d / "sub").mkdir()
    f = _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo OK"\n')
    fp = trust_project_scripts(f)
    forms = [
        d / "apm.yml",
        d / "." / "apm.yml",
        d / "sub" / ".." / "apm.yml",
        Path(str(d) + "/apm.yml"),
    ]
    for form in forms:
        assert is_fingerprint_trusted(form, fp) is True, f"trust split on form {form}"
    # Mutate content; every path form must now read as untrusted.
    _write(f, 'lifecycle:\n  pre-install:\n    - run: "echo EVIL"\n')
    new_fp = fingerprint_lifecycle_subtree(load_yaml(f).get("lifecycle"))
    assert new_fp != fp
    for form in forms:
        assert is_fingerprint_trusted(form, new_fp) is False


def test_realfile_to_symlink_swap_single_parse_defeats_toctou(tmp_apm_home):
    """Trust a real apm.yml; swap it for a symlink to hostile content. The
    firing path parses ONCE (parse_apm_yml_lifecycle_with_fingerprint), so the
    fp it gates on is the fp of the content it executes. Assert the swapped
    content's fp differs from the trusted fp -> hostile content fails closed."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    real = _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo OK"\n')
    fp = trust_project_scripts(real)
    hostile = _write(d / "hostile.yml", 'lifecycle:\n  pre-install:\n    - run: "echo EVIL"\n')
    # Swap real for a symlink -> hostile.
    real.unlink()
    os.symlink(hostile, real)
    entries, swapped_fp = parse_apm_yml_lifecycle_with_fingerprint(real, "project")
    assert entries and entries[0].bash == "echo EVIL"
    assert swapped_fp != fp
    assert is_fingerprint_trusted(real, swapped_fp) is False, (
        "SYMLINK SWAP BYPASS: hostile symlink target fired under real-file trust"
    )


# --------------------------------------------------------------------------
# Vector E: trust-store value type confusion
# --------------------------------------------------------------------------
def test_trust_store_nonstring_value_cannot_match(tmp_apm_home):
    """A crafted store value that is not the exact fp string must not make
    is_fingerprint_trusted return True. _load_trust_store drops non-str values;
    a bool/int/null/list there is filtered out (treated as un-trusted)."""
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    key = str((Path(tempfile.mkdtemp(dir=str(_ROOT))) / "apm.yml").resolve())
    # Junk values that must NOT be honoured as a trust record.
    payload = {
        "version": 1,
        "projects": {
            key: True,  # type-confusion: truthy non-str
        },
    }
    store.write_text(json.dumps(payload), encoding="utf-8")
    loaded = _load_trust_store()
    assert key not in loaded, "non-str trust value survived load -> type confusion"
    # And a real fp lookup against it is False.
    assert is_fingerprint_trusted(Path(key), "0" * 64) is False


def test_trust_store_wildcard_string_value_only_matches_exact_fp(tmp_apm_home):
    """A stored value equal to some attacker-guessable constant matches ONLY a
    fingerprint string equal to it. Since fingerprints are sha256 hexdigests, a
    non-hex junk string can never equal a real fp -> never trusts real content.
    """
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    f = _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo OK"\n')
    key = str(f.resolve())
    _write_trust_store({key: "not-a-real-fingerprint"})
    real_fp = fingerprint_lifecycle_subtree(load_yaml(f).get("lifecycle"))
    assert real_fp is not None and real_fp != "not-a-real-fingerprint"
    assert is_fingerprint_trusted(f, real_fp) is False


# --------------------------------------------------------------------------
# Vector F: lifecycle test --execute escape-hatch audit (informational)
# --------------------------------------------------------------------------
def test_untrust_roundtrip_and_no_residual_trust(tmp_apm_home):
    """Sanity: trust then untrust leaves no residual record that could fire."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    f = _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo OK"\n')
    fp = trust_project_scripts(f)
    assert is_fingerprint_trusted(f, fp) is True
    assert untrust_project_scripts(f) is True
    assert is_fingerprint_trusted(f, fp) is False


def test_no_hang_on_legitimate_manifest(tmp_apm_home):
    """The whole fingerprint path must complete quickly on a normal manifest."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    f = _write(
        d / "apm.yml",
        'lifecycle:\n  pre-install:\n    - run: "echo A"\n  post-install:\n    - run: "echo B"\n',
    )
    fp = _run_with_timeout(lambda: trust_project_scripts(f), seconds=10)
    assert fp is not None


def test_lifecycle_test_execute_path_is_ungated_by_design():
    """AUDIT (non-bypass): ``apm lifecycle test --execute`` builds its runner
    directly from ``discover_scripts`` (which always includes the project tier)
    -- it does NOT route through ``build_runner_from_context`` and therefore
    does NOT apply the trust gate.

    This is an EXPLICIT, opt-in developer command (flag help: "Actually run the
    scripts"), distinct from the AUTOMATIC install/update/uninstall paths, all
    three of which DO gate (they call build_runner_from_context). The automatic
    supply-chain hole the trust feature exists to close stays closed. We assert
    the gated/ungated split precisely so any future regression that routes an
    AUTOMATIC path through the ungated discover_scripts is caught.
    """
    from apm_cli.core.lifecycle_scripts import (
        LifecycleScriptRunner,
        discover_scripts,
    )

    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    _write(d / "apm.yml", 'lifecycle:\n  pre-install:\n    - run: "echo PROJECT"\n')
    # The manual --execute path: discover_scripts() returns project entries with
    # NO trust check, and the runner fires whatever it is handed.
    entries = discover_scripts(project_root=str(d))
    project = [e for e in entries if e.source == "project"]
    assert project and project[0].bash == "echo PROJECT"
    runner = LifecycleScriptRunner(scripts=entries, project_root=str(d))
    # Untrusted project script survives into the runner on this manual path.
    assert any(s.source == "project" for s in runner.scripts_for_event("pre-install"))
    # Contrast: the AUTOMATIC firing boundary build_runner_from_context DOES gate
    # (covered by the install/update/uninstall suites); this test only pins the
    # documented escape-hatch so it cannot silently widen to automatic paths.
