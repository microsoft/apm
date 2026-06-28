"""Round-21 red-team probes of the project-script trust model.

Targets four lateral vectors the priming flagged as the crux:

1. HASH-DOMAIN vs EXEC-DOMAIN drift via a top-level YAML anchor referenced
   from inside the ``lifecycle:`` subtree (anchor-outside-subtree).
2. Case-insensitive path over-trust on a case-preserving fs (macOS APFS).
3. Single-parse identity: the executed object IS the hashed object.
4. Path+fingerprint are AND-ed (clone-B-at-same-path must re-gate).

Every probe asserts the SECURE contract: untrusted/mutated content must NOT
execute, and any ambiguity must fail CLOSED (untrusted), never fail OPEN.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apm_cli.core import lifecycle_scripts as ls
from apm_cli.core.lifecycle_scripts import (
    parse_apm_yml_lifecycle_with_fingerprint,
)
from apm_cli.core.script_trust import (
    fingerprint_lifecycle_subtree,
    is_fingerprint_trusted,
    trust_project_scripts,
)
from apm_cli.utils.yaml_io import load_yaml


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir="/tmp/rt21-trust")
    monkeypatch.setenv("APM_HOME", home)
    yield Path(home)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# Vector 1: top-level anchor referenced inside lifecycle (hash/exec drift)
# --------------------------------------------------------------------------
def test_anchor_outside_subtree_is_baked_into_fingerprint(tmp_apm_home):
    """Mutating a top-level anchor that lifecycle: aliases MUST change the fp.

    PyYAML resolves aliases at construction, so the subtree extracted by
    load_yaml()['lifecycle'] is POST-resolution: the anchor value is baked
    in. If extraction were pre-resolution the anchor would be a hole.
    """
    d = Path(tempfile.mkdtemp(dir="/tmp/rt21-trust"))
    benign = _write(
        d / "benign" / "apm.yml",
        'top: &cmd "echo TRUSTED"\nlifecycle:\n  pre-install:\n    - run: *cmd\n',
    )
    evil = _write(
        d / "evil" / "apm.yml",
        'top: &cmd "echo MALICIOUS"\nlifecycle:\n  pre-install:\n    - run: *cmd\n',
    )

    benign_entries, benign_fp = parse_apm_yml_lifecycle_with_fingerprint(benign, "project")
    evil_entries, evil_fp = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")

    # The aliased value is materialised inside the executed entry...
    assert benign_entries[0].command == "echo TRUSTED"
    assert evil_entries[0].command == "echo MALICIOUS"
    # ...and the fingerprint reflects it: mutating the anchor revokes trust.
    assert benign_fp is not None
    assert benign_fp != evil_fp, "anchor mutation outside lifecycle did NOT revoke trust"

    # Trust benign; the evil manifest (same alias name, swapped value) must
    # NOT be trusted under benign's fingerprint.
    assert is_fingerprint_trusted(benign, benign_fp) is False  # not trusted yet
    trust_project_scripts(benign)
    assert is_fingerprint_trusted(benign, benign_fp) is True
    assert is_fingerprint_trusted(evil, evil_fp) is False


# --------------------------------------------------------------------------
# Vector 2: case-insensitive path over-trust on a case-preserving fs
# --------------------------------------------------------------------------
def test_case_variant_path_does_not_over_trust(tmp_apm_home):
    """Trusting via one casing must NOT bless a different-cased access.

    On APFS (case-insensitive, case-preserving) resolve() preserves case, so
    a case-variant path yields a DIFFERENT trust key -> fail-closed. The only
    way a case-variant could be 'trusted' is if it is the SAME file with the
    SAME fingerprint, which is not a bypass. Assert fail-closed direction.
    """
    d = Path(tempfile.mkdtemp(dir="/tmp/rt21-trust"))
    real = _write(d / "RepoCase" / "apm.yml", "lifecycle:\n  pre-install:\n    - run: echo hi\n")

    _, fp = parse_apm_yml_lifecycle_with_fingerprint(real, "project")
    assert fp is not None

    # Trust via a DIFFERENT casing of the same on-disk file.
    variant = d / "repocase" / "apm.yml"
    assert variant.exists(), "fs is case-sensitive; case probe N/A on this host"
    trust_project_scripts(variant)

    # Firing reads the on-disk (preserved) case. Keys differ -> NOT trusted.
    # SECURE: a casing mismatch fails CLOSED (re-gate), never over-trusts.
    real_key = str(real.resolve())
    variant_key = str(variant.resolve())
    if real_key != variant_key:
        assert is_fingerprint_trusted(real, fp) is False
    else:
        # If a host DID case-fold, it is the same key+same file+same fp:
        # execution-equivalent, still not a bypass.
        assert is_fingerprint_trusted(real, fp) is True


# --------------------------------------------------------------------------
# Vector 3: single-parse -> executed object IS the hashed object
# --------------------------------------------------------------------------
def test_executed_object_is_hashed_object_single_parse(tmp_apm_home, monkeypatch):
    """The entries that execute and the fp that gates them come from ONE parse.

    Patch load_yaml to count reads; parse_apm_yml_lifecycle_with_fingerprint
    must call it exactly once, and the fp must match a re-fingerprint of the
    very object the entries were built from.
    """
    apm = _write(
        Path(tempfile.mkdtemp(dir="/tmp/rt21-trust")) / "apm.yml",
        "lifecycle:\n  pre-install:\n    - run: echo same-object\n",
    )

    calls = {"n": 0}
    captured = {}
    real_load = ls.load_yaml if hasattr(ls, "load_yaml") else load_yaml

    def counting_load(path):
        calls["n"] += 1
        data = real_load(path)
        captured["data"] = data
        return data

    # parse_apm_yml_lifecycle_with_fingerprint imports load_yaml locally from
    # apm_cli.utils.yaml_io, so patch it at the source module.
    monkeypatch.setattr("apm_cli.utils.yaml_io.load_yaml", counting_load)

    entries, fp = parse_apm_yml_lifecycle_with_fingerprint(apm, "project")
    assert calls["n"] == 1, "more than one parse opens a TOCTOU window"
    # The fp equals a re-hash of the SAME in-memory subtree the entries used.
    same_obj_fp = fingerprint_lifecycle_subtree(captured["data"]["lifecycle"])
    assert fp == same_obj_fp
    assert entries[0].command == "echo same-object"


# --------------------------------------------------------------------------
# Vector 4: path AND fingerprint (clone-B-at-same-path must re-gate)
# --------------------------------------------------------------------------
def test_same_path_different_content_re_gates(tmp_apm_home):
    """Clone A trusted; clone B with different lifecycle at same path re-gates."""
    d = Path(tempfile.mkdtemp(dir="/tmp/rt21-trust"))
    apm = _write(d / "apm.yml", "lifecycle:\n  pre-install:\n    - run: echo A\n")
    _, fp_a = parse_apm_yml_lifecycle_with_fingerprint(apm, "project")
    trust_project_scripts(apm)
    assert is_fingerprint_trusted(apm, fp_a) is True

    # Same path, new (untrusted) content B.
    apm.write_text("lifecycle:\n  pre-install:\n    - run: echo B\n", encoding="utf-8")
    _, fp_b = parse_apm_yml_lifecycle_with_fingerprint(apm, "project")
    assert fp_b != fp_a
    # SECURE: the stale path record must NOT bless B.
    assert is_fingerprint_trusted(apm, fp_b) is False


# --------------------------------------------------------------------------
# Vector 5: fail-CLOSED audit -- bomb / unfingerprintable -> untrusted
# --------------------------------------------------------------------------
def test_unfingerprintable_subtree_fails_closed(tmp_apm_home):
    """A subtree whose fp is None must never be considered trusted."""
    apm = _write(Path(tempfile.mkdtemp(dir="/tmp/rt21-trust")) / "apm.yml", "lifecycle: {}\n")
    # fingerprint None (empty/unsafe) -> is_fingerprint_trusted returns False
    assert is_fingerprint_trusted(apm, None) is False
    # Even after trusting, a None fp at fire time fails closed.
    apm.write_text("lifecycle:\n  pre-install:\n    - run: echo x\n", encoding="utf-8")
    trust_project_scripts(apm)
    assert is_fingerprint_trusted(apm, None) is False


# --------------------------------------------------------------------------
# Vector 1b: merge-key (<<) importing an OUTSIDE mapping into a lifecycle entry
# --------------------------------------------------------------------------
def test_merge_key_outside_value_is_baked_into_fingerprint(tmp_apm_home):
    """A lifecycle entry built via '<<: *outside' bakes the merged value in.

    Merge keys resolve at parse, so the imported command lands inside the
    subtree object that is both hashed and executed. Mutating the outside
    mapping must change the fingerprint (re-gate), not silently swap exec.
    """
    d = Path(tempfile.mkdtemp(dir="/tmp/rt21-trust"))
    benign = _write(
        d / "b" / "apm.yml",
        "defs: &d {run: echo TRUSTED}\nlifecycle:\n  pre-install:\n    - <<: *d\n",
    )
    evil = _write(
        d / "e" / "apm.yml",
        "defs: &d {run: echo PWNED}\nlifecycle:\n  pre-install:\n    - <<: *d\n",
    )
    b_entries, b_fp = parse_apm_yml_lifecycle_with_fingerprint(benign, "project")
    e_entries, e_fp = parse_apm_yml_lifecycle_with_fingerprint(evil, "project")
    assert b_entries[0].command == "echo TRUSTED"
    assert e_entries[0].command == "echo PWNED"
    assert b_fp is not None and b_fp != e_fp
    trust_project_scripts(benign)
    assert is_fingerprint_trusted(benign, b_fp) is True
    assert is_fingerprint_trusted(evil, e_fp) is False
