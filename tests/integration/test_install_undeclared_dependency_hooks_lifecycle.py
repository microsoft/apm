"""Install retires the merged hooks of a dependency apm.yml no longer declares (#5057).

PROMISE: P-OWN-orphan-package-selection -- when ``apm install`` finds that a
dependency is no longer declared, it removes the merged hook entries APM
recorded for that dependency, as ``apm uninstall`` does, and keeps every entry
APM did not write (source: #5057 scope).
CELL: OP=install SRC=local PRIM=hook(merged) SCOPE=project TGT=claude
GRAPH=direct-dependency-removed-from-manifest
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I1", "P-INV-I2"),
]

_USER_ENTRY = {"matcher": "Mine", "hooks": [{"type": "command", "command": "true"}]}


def _hook_package(root: Path, name: str, matcher: str) -> None:
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    hook = {"matcher": matcher, "hooks": [{"type": "command", "command": f"echo {name}"}]}
    write(root / ".apm" / "hooks" / f"{name}.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))


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


def _entries(world: ReplayWorld) -> dict[str, list[str]]:
    data = json.loads((world.project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return {
        event: [entry.get("matcher") for entry in entries]
        for event, entries in data.get("hooks", {}).items()
    }


def test_install_retires_the_hooks_of_an_undeclared_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The removed package's entry goes; the user's and a survivor's entries stay."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _hook_package(world.project / "vendor" / "h", "h", "Bash")
    _hook_package(world.project / "vendor" / "k", "k", "Write")
    write(
        world.project / ".claude" / "settings.json",
        json.dumps({"hooks": {"Stop": [_USER_ENTRY]}}),
    )
    _manifest(world, ["h", "k"])
    world.apm("install")
    assert _entries(world) == {"Stop": ["Mine"], "PreToolUse": ["Bash", "Write"]}

    _manifest(world, ["k"])
    world.apm("install")

    assert _entries(world) == {"Stop": ["Mine"], "PreToolUse": ["Write"]}
    sidecar = (world.project / ".claude" / "apm-hooks.json").read_text(encoding="utf-8")
    assert "_local/h" not in sidecar, sidecar
    # Converged: a frozen replay and a second install change nothing.
    settled = (world.project / ".claude" / "settings.json").read_bytes(), lock_bytes(world.project)
    world.apm("install", "--frozen")
    world.apm("install")
    assert (
        (world.project / ".claude" / "settings.json").read_bytes(),
        lock_bytes(world.project),
    ) == settled


def test_install_keeps_an_edited_entry_of_an_undeclared_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An entry the user edited is no longer APM's, so it stays."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _hook_package(world.project / "vendor" / "h", "h", "Bash")
    (world.project / ".claude").mkdir()
    _manifest(world, ["h"])
    world.apm("install")
    settings = world.project / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    _manifest(world, [])
    world.apm("install")

    assert _entries(world) == {"PreToolUse": ["Edit"]}
