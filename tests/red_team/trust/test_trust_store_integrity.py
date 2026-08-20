"""Vector 2 -- trust-store integrity: malformed stores must fail CLOSED.

A corrupt, attacker-shaped, or wrong-typed trust store must never grant
trust.  These assert the secure (fail-closed) behavior, so they are
regression traps that pass on head.  The world-writable / planted-entry
case documents the accepted threat-model boundary (root of trust).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from apm_cli.core.script_trust import (
    is_project_scripts_trusted,
    script_file_fingerprint,
    trust_project_scripts,
)

BENIGN = 'name: pkg\nlifecycle:\n  post-install:\n    - type: command\n      run: "true"\n'


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    apm_yml = project / "apm.yml"
    apm_yml.write_text(BENIGN, encoding="utf-8")
    return apm_yml


MALFORMED_STORES = [
    "this is not json {{{",
    "[]",
    "null",
    '"a string"',
    "123",
    json.dumps({"version": 1}),  # no projects key
    json.dumps({"version": 1, "projects": []}),  # projects not a dict
    json.dumps({"version": 1, "projects": "nope"}),  # projects wrong type
]


@pytest.mark.parametrize("payload", MALFORMED_STORES)
def test_malformed_store_fails_closed(apm_home: Path, tmp_path: Path, payload: str) -> None:
    """Any non-conforming store yields no trust."""
    apm_yml = _project(tmp_path)
    (apm_home / "scripts-trust.json").write_text(payload, encoding="utf-8")
    assert not is_project_scripts_trusted(apm_yml)


def test_projects_value_wrong_type_is_ignored(apm_home: Path, tmp_path: Path) -> None:
    """A non-string fingerprint value for the project key is dropped (no trust)."""
    apm_yml = _project(tmp_path)
    key = str(apm_yml.resolve())
    (apm_home / "scripts-trust.json").write_text(
        json.dumps({"version": 1, "projects": {key: 12345}}), encoding="utf-8"
    )
    assert not is_project_scripts_trusted(apm_yml)


def test_planted_correct_entry_grants_trust(apm_home: Path, tmp_path: Path) -> None:
    """A directly-planted, correctly-shaped entry grants trust.

    This is the accepted threat-model boundary: the trust store IS the root
    of trust.  Anyone who can write a valid entry into it has, by design,
    already won.  Documented residual risk, not a gate defect.
    """
    apm_yml = _project(tmp_path)
    fp = script_file_fingerprint(apm_yml)
    assert fp is not None
    key = str(apm_yml.resolve())
    (apm_home / "scripts-trust.json").write_text(
        json.dumps({"version": 1, "projects": {key: fp}}), encoding="utf-8"
    )
    assert is_project_scripts_trusted(apm_yml)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_trust_store_written_0600(apm_home: Path, tmp_path: Path) -> None:
    """trust_project_scripts persists the store with 0o600 perms."""
    apm_yml = _project(tmp_path)
    trust_project_scripts(apm_yml)
    store = apm_home / "scripts-trust.json"
    mode = stat.S_IMODE(store.stat().st_mode)
    assert mode == 0o600, f"trust store should be 0o600, got {oct(mode)}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_apm_home_parent_dir_permissions_not_enforced(apm_home: Path, tmp_path: Path) -> None:
    """RESIDUAL RISK: the APM_HOME dir mode is NOT hardened on write.

    trust_project_scripts only chmods the file, never the parent dir.  If
    $APM_HOME is group/other-writable an attacker can replace the 0o600
    file wholesale.  This documents (does not fail) that gap: we assert the
    parent dir mode is left untouched by the trust write.
    """
    apm_yml = _project(tmp_path)
    os.chmod(apm_home, 0o777)  # noqa: S103 -- intentional: model an unsafe APM_HOME
    trust_project_scripts(apm_yml)
    parent_mode = stat.S_IMODE(apm_home.stat().st_mode)
    assert parent_mode == 0o777, (
        "documents that APM_HOME perms are not re-hardened by the trust write"
    )
