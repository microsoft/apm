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


def _world(
    tmp_path: Path, apm_binary_path: Path, event: str = "PreToolUse"
) -> tuple[ReplayWorld, Path]:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    package = world.project / "vendor" / "h"
    write(package / "apm.yml", "name: h\nversion: 1.0.0\n")
    hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo h"}]}
    write(package / ".apm" / "hooks" / "h.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))
    (world.project / ".claude").mkdir()
    _manifest(world, ["h"])
    world.apm("install")
    settings = world.project / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {event: None}}) + "\n", encoding="utf-8")
    return world, settings


@pytest.mark.parametrize("event", ["PreToolUse", "preToolUse"], ids=["event", "alias"])
def test_install_names_a_malformed_event_it_must_merge_into(
    tmp_path: Path, apm_binary_path: Path, event: str
) -> None:
    """Merging a hook into a null event, or a null alias it clears, names that spelling."""
    world, settings = _world(tmp_path, apm_binary_path, event)
    before = settings.read_bytes()

    result = world.apm("install", expect=1)

    text = " ".join((result.stdout + result.stderr).split())
    assert "NoneType" not in text and "Traceback" not in text, text
    expected = (
        f".claude/settings.json: hook event '{event}' is not a list of hook entries. "
        "Fix or remove the event, then run 'apm install' to"
    )
    assert expected in text, text
    assert settings.read_bytes() == before
    # The printed remedy: fix the event, then run apm install again.
    settings.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
    world.apm("install")
    assert "echo h" in settings.read_text(encoding="utf-8")


def test_install_leaves_a_malformed_event_it_does_not_need(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """With the hook package removed, install completes and keeps the user's bytes."""
    world, settings = _world(tmp_path, apm_binary_path)
    before = settings.read_bytes()
    _manifest(world, [])

    result = world.apm("install", expect=None)

    text = " ".join((result.stdout + result.stderr).split())
    assert "NoneType" not in text and "Traceback" not in text, text
    assert result.returncode == 0, text
    assert settings.read_bytes() == before


def test_user_scope_install_names_the_scoped_remedy(tmp_path: Path, apm_binary_path: Path) -> None:
    """At user scope the remedy is 'apm install -g', which retries the failed merge."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    package = world.isolated.work_root / "h"
    write(package / "apm.yml", "name: h\nversion: 1.0.0\n")
    hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo h"}]}
    write(package / ".apm" / "hooks" / "h.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))
    home = world.isolated.home
    dump_yaml(
        {
            "name": "user",
            "version": "0.1.0",
            "targets": ["claude"],
            "dependencies": {"apm": [str(package)]},
            "executables": {"allow": {str(package): {"hooks": True}}},
        },
        home / ".apm" / "apm.yml",
    )
    (home / ".claude").mkdir(exist_ok=True)
    world.apm("install", "-g", cwd=world.isolated.work_root)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": None}}) + "\n", encoding="utf-8")

    result = world.apm("install", "-g", cwd=world.isolated.work_root, expect=1)

    text = " ".join((result.stdout + result.stderr).split())
    assert "is not a list of hook entries" in text, text
    assert "then run 'apm install -g' to" in text, text
    settings.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
    world.apm("install", "-g", cwd=world.isolated.work_root)
    assert "echo h" in settings.read_text(encoding="utf-8")
