"""A malformed event in a deployed hook config never aborts install (#5137).

PROMISE: P-INV-I7 -- nothing fails with a bare internal error: when the
deployed ``.claude/settings.json`` holds a hook event that is not a list (for
example ``"PreToolUse": null``) and APM's ownership sidecar still records
that event, ``apm install`` completes, leaves the malformed value as the user
wrote it, and names the event (source: #5137).
CELL: OP=install SRC=local PRIM=hook(merged) SCOPE=project TGT=claude
DEPLOYED=malformed-event
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]


def _manifest(world: ReplayWorld, names: list[str]) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [f"./vendor/{name}" for name in names]},
            "executables": {"allow": {f"./vendor/{name}": {"hooks": True} for name in names}},
        },
        world.project / "apm.yml",
    )


@pytest.mark.parametrize("declared", [["h"], []], ids=["still-declared", "removed"])
def test_install_names_a_malformed_deployed_hook_event(
    tmp_path: Path, apm_binary_path: Path, declared: list[str]
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    package = world.project / "vendor" / "h"
    write(package / "apm.yml", "name: h\nversion: 1.0.0\n")
    hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo h"}]}
    write(package / ".apm" / "hooks" / "h.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))
    (world.project / ".claude").mkdir()
    _manifest(world, ["h"])
    world.apm("install")
    settings = world.project / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": None}}) + "\n", encoding="utf-8")
    _manifest(world, declared)

    result = world.apm("install", expect=None)

    text = " ".join((result.stdout + result.stderr).split())
    assert "NoneType" not in text and "Traceback" not in text, text
    assert result.returncode == 0, text
    assert "PreToolUse" in text and ".claude/settings.json" in text, text
