"""Round-26 red-team probes of the lifecycle-script TRUST surface.

Eighteenth-round hunt for a GENUINE trust break the prior 17 clean rounds
(r9-r25) missed. A genuine break is exactly one of:

  * FAIL-OPEN  -- an untrusted / un-fingerprintable project subtree FIRES;
  * UNWANTED-FIRE -- a project subtree the developer never trusted FIRES;
  * TRUSTED-BUT-CHANGED-EXEC -- a trusted record survives a security-relevant
    edit to the EXECUTED bytes (hash-domain != exec-domain), so different
    commands run under a stale "trusted" verdict.

A DoS that fails CLOSED (legit subtree becomes un-trustable -> scripts skipped)
is a trust WIN, not a break, and is routed to the parser agent -- NOT asserted
as a break here.

Novel r26 pivots (not re-treading r21-r25 beyond confirming the trap holds on
head d37c55997):

  A. CROSS-BOUNDARY NESTED ALIAS CHAIN -- a two-hop alias chain whose root is
     defined OUTSIDE ``lifecycle:`` and is pulled into an executed command.
     Mutating the out-of-subtree root MUST rebake the subtree-only fingerprint
     (the RESOLVED, expanded value is what is hashed and executed).
  B. EVENT-KEY REORDER INDEPENDENCE -- json.dumps(sort_keys=True) makes two
     subtrees that differ ONLY in top-level event-key order share a
     fingerprint. That is SAFE iff per-event execution is identical (each event
     fires its own order-preserved list). Proven equal -> no exec-confusion.
  C. macOS /tmp -> /private/tmp realpath key-collapse -- trust written via the
     ``/tmp`` alias and checked via the ``/private`` canonical path must key to
     the SAME record (same file), while a DIFFERENT file never inherits it.
  D. SYMLINK SWAP AFTER TRUST -- trust a real file, then replace it with a
     symlink to a malicious file: the resolved key changes -> fail CLOSED ->
     the malicious command does NOT fire through build_runner_from_context.
  E. PROJECT_ROOT NORMALISATION -- trailing-slash / ``.`` / ``..`` forms of the
     trusted root fire; an untrusted sibling project never fires.
  F. RELATIVE-KEY STORE POISON -- a relative path key planted in the store does
     NOT satisfy the absolute-resolved lookup (no pre-trust by relative key).
  G. FALSE-POSITIVE GUARD -- editing ``dependencies:`` (outside lifecycle:)
     keeps trust; editing ``lifecycle:`` revokes it, end-to-end.

Hangs are caught with a daemon thread + join(timeout) (the runtime bans the
``timeout`` shell command, kill, pkill).
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
    build_runner_from_context,
    parse_apm_yml_lifecycle_with_fingerprint,
)
from apm_cli.core.script_trust import (
    _load_trust_store,
    _trust_store_path,
    fingerprint_lifecycle_subtree,
    is_fingerprint_trusted,
    trust_project_scripts,
)

_ROOT = Path(tempfile.mkdtemp(prefix="rt26-trust-"))


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    yield Path(home)


def _proj(text: str) -> Path:
    """Write apm.yml into a fresh dir, return its path."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    yml = d / "apm.yml"
    yml.write_text(text, encoding="utf-8")
    return yml


def _run_with_timeout(fn, seconds: float = 20.0):
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


def _cmds(entries) -> list[str | None]:
    return [s.effective_command for s in entries]


# -- A. cross-boundary nested alias chain MUST rebake -----------------------


def test_chained_merge_across_boundary_rebakes():
    """A two-level ``<<`` merge chain whose DEEPEST source lives outside the
    lifecycle subtree is pulled (via flatten_mapping) into an executed entry.
    Mutating the deepest out-of-subtree source MUST rebake the subtree-only
    fingerprint -- the resolved (flattened) value is what is hashed/executed.
    """
    tmpl = (
        "deep: &deep\n"
        "  run: echo {who}\n"
        "mid: &mid\n"
        "  <<: *deep\n"
        "lifecycle:\n"
        "  post-install:\n"
        "    - <<: *mid\n"
    )
    safe = _proj(tmpl.format(who="SAFE"))
    evil = _proj(tmpl.format(who="EVIL"))
    e_safe, f_safe = parse_apm_yml_lifecycle_with_fingerprint(safe, "project")
    e_evil, f_evil = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")

    assert _cmds(e_safe) == ["echo SAFE"]
    assert _cmds(e_evil) == ["echo EVIL"]
    assert f_safe is not None and f_evil is not None
    # TRUSTED-BUT-CHANGED-EXEC would be f_safe == f_evil. Must differ.
    assert f_safe != f_evil


# -- B. event-key reorder shares fp but execution is identical --------------


def test_event_key_reorder_same_fp_same_per_event_execution():
    """Two subtrees differing only in top-level event-key ORDER share a
    fingerprint (sort_keys). That is a bypass ONLY if per-event execution
    differs. It does not: each event fires its own order-preserved list.
    """
    a = {
        "pre-install": [{"run": "echo PRE"}],
        "post-install": [{"run": "echo POST"}],
    }
    b = {
        "post-install": [{"run": "echo POST"}],
        "pre-install": [{"run": "echo PRE"}],
    }
    fa = fingerprint_lifecycle_subtree(a)
    fb = fingerprint_lifecycle_subtree(b)
    assert fa is not None and fa == fb  # benign reorder -> stable fp

    proj = Path(tempfile.mkdtemp(dir=str(_ROOT))) / "apm.yml"
    ea = _entries_from_lifecycle_map(a, proj, "project")
    eb = _entries_from_lifecycle_map(b, proj, "project")
    pre_a = [s.effective_command for s in ea if s.event == "pre-install"]
    post_a = [s.effective_command for s in ea if s.event == "post-install"]
    pre_b = [s.effective_command for s in eb if s.event == "pre-install"]
    post_b = [s.effective_command for s in eb if s.event == "post-install"]
    # Same fp => per-event executed command lists must match (no confusion).
    assert pre_a == pre_b == ["echo PRE"]
    assert post_a == post_b == ["echo POST"]


# -- C. /tmp -> /private/tmp realpath collapses to ONE trust key ------------


def test_tmp_private_realpath_collapse_same_record_only(tmp_apm_home):
    """On macOS /tmp is a symlink to /private/tmp. Trust written via one alias
    and checked via the canonical path must key to the SAME record (same file),
    and a DIFFERENT file must NOT inherit that trust.
    """
    if not Path("/tmp").is_symlink():
        pytest.skip("/tmp is not a symlink on this platform")

    # Place the manifest under /tmp so the two aliases differ pre-resolve.
    d = Path(tempfile.mkdtemp(prefix="rt26c-", dir="/tmp"))
    yml_via_tmp = d / "apm.yml"
    yml_via_tmp.write_text("lifecycle:\n  post-install:\n    - run: echo SAFE\n", encoding="utf-8")
    canonical = yml_via_tmp.resolve()
    assert str(canonical) != str(yml_via_tmp)  # /tmp vs /private/tmp differ

    fp = trust_project_scripts(yml_via_tmp)
    assert fp is not None
    # Checked via the canonical path: same file, same fp -> trusted.
    assert is_fingerprint_trusted(canonical, fp) is True
    # Checked via the original /tmp alias: also trusted (resolve collapses).
    assert is_fingerprint_trusted(yml_via_tmp, fp) is True

    # A DIFFERENT file with identical CONTENT must NOT inherit the trust
    # (trust is keyed to the resolved PATH, not just the fingerprint).
    other = _proj("lifecycle:\n  post-install:\n    - run: echo SAFE\n")
    other_fp = parse_apm_yml_lifecycle_with_fingerprint(other, "project")[1]
    assert other_fp == fp  # identical content -> identical fingerprint
    assert is_fingerprint_trusted(other, other_fp) is False


# -- D. symlink swap after trust fails CLOSED end-to-end --------------------


def test_symlink_swap_after_trust_fails_closed(tmp_apm_home):
    """Trust a REAL apm.yml, then replace it with a symlink to a malicious
    apm.yml. The resolved trust key changes -> not trusted -> malicious scripts
    are SKIPPED by build_runner_from_context (no unwanted fire)."""
    proj_dir = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    real = proj_dir / "apm.yml"
    real.write_text("lifecycle:\n  post-install:\n    - run: echo SAFE\n", encoding="utf-8")

    assert trust_project_scripts(real) is not None
    r_trusted = _run_with_timeout(lambda: build_runner_from_context(project_root=str(proj_dir)))
    assert _cmds(r_trusted._scripts) == ["echo SAFE"]  # trust enables

    # Attacker swaps the trusted path for a symlink to a malicious file.
    evil_dir = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    evil = evil_dir / "evil.yml"
    evil.write_text("lifecycle:\n  post-install:\n    - run: echo EVIL\n", encoding="utf-8")
    real.unlink()
    os.symlink(str(evil), str(real))

    r_after = _run_with_timeout(lambda: build_runner_from_context(project_root=str(proj_dir)))
    # The malicious command must NOT fire under the original file's trust.
    assert "echo EVIL" not in _cmds(r_after._scripts)
    assert r_after._scripts == []
    assert r_after._skipped_project_scripts == 1


# -- E. project_root normalisation: trusted forms fire, sibling never --------


def test_project_root_normalised_forms_fire_sibling_never(tmp_apm_home):
    proj_dir = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    yml = proj_dir / "apm.yml"
    yml.write_text("lifecycle:\n  post-install:\n    - run: echo SAFE\n", encoding="utf-8")
    assert trust_project_scripts(yml) is not None

    base = proj_dir.name
    forms = [
        str(proj_dir),
        str(proj_dir) + "/",
        str(proj_dir / "."),
        str(proj_dir.parent / base / "."),
        str(proj_dir / ".." / base),
    ]
    for form in forms:
        r = _run_with_timeout(lambda f=form: build_runner_from_context(project_root=f))
        assert _cmds(r._scripts) == ["echo SAFE"], f"trusted form did not fire: {form}"

    # A sibling project with identical content but no trust record never fires.
    sib_dir = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    (sib_dir / "apm.yml").write_text(
        "lifecycle:\n  post-install:\n    - run: echo SAFE\n", encoding="utf-8"
    )
    r_sib = _run_with_timeout(lambda: build_runner_from_context(project_root=str(sib_dir)))
    assert r_sib._scripts == []
    assert r_sib._skipped_project_scripts == 1


# -- F. relative-key store poison cannot pre-trust --------------------------


def test_relative_key_store_poison_does_not_pretrust(tmp_apm_home):
    """An attacker who can only plant a RELATIVE-path key in the store cannot
    satisfy the absolute-resolved lookup, so no project is silently pre-trusted.
    """
    yml = _proj("lifecycle:\n  post-install:\n    - run: echo SAFE\n")
    fp = parse_apm_yml_lifecycle_with_fingerprint(yml, "project")[1]
    assert fp is not None

    # Plant a relative-key record (e.g. "apm.yml" or "./apm.yml") in the store.
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    poisoned = {
        "version": 1,
        "projects": {"apm.yml": fp, "./apm.yml": fp, yml.name: fp},
    }
    store.write_text(json.dumps(poisoned), encoding="utf-8")
    assert _load_trust_store()  # store loads (keys are valid str)

    # The absolute-resolved lookup must NOT match any relative planted key.
    assert is_fingerprint_trusted(yml, fp) is False


# -- G. false-positive guard: dependency edit keeps trust; lifecycle revokes -


def test_dependency_edit_keeps_trust_lifecycle_edit_revokes(tmp_apm_home):
    proj_dir = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    yml = proj_dir / "apm.yml"
    yml.write_text(
        "dependencies:\n  - some/pkg@v1\nlifecycle:\n  post-install:\n    - run: echo SAFE\n",
        encoding="utf-8",
    )
    assert trust_project_scripts(yml) is not None
    r0 = _run_with_timeout(lambda: build_runner_from_context(project_root=str(proj_dir)))
    assert _cmds(r0._scripts) == ["echo SAFE"]

    # Benign edit OUTSIDE lifecycle: trust must SURVIVE (no re-gate).
    yml.write_text(
        "dependencies:\n  - some/pkg@v1\n  - other/pkg@v2\n"
        "lifecycle:\n  post-install:\n    - run: echo SAFE\n",
        encoding="utf-8",
    )
    r1 = _run_with_timeout(lambda: build_runner_from_context(project_root=str(proj_dir)))
    assert _cmds(r1._scripts) == ["echo SAFE"], "benign dependency edit wrongly re-gated"

    # Security-relevant edit INSIDE lifecycle: trust must REVOKE (fail closed).
    yml.write_text(
        "dependencies:\n  - some/pkg@v1\n  - other/pkg@v2\n"
        "lifecycle:\n  post-install:\n    - run: echo EVIL\n",
        encoding="utf-8",
    )
    r2 = _run_with_timeout(lambda: build_runner_from_context(project_root=str(proj_dir)))
    assert "echo EVIL" not in _cmds(r2._scripts)
    assert r2._scripts == []
    assert r2._skipped_project_scripts == 1
