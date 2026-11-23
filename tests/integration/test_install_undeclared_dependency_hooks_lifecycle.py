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
import os
import sys
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


def _script_hook_package(root: Path, name: str) -> None:
    """A hook that runs a packaged script, deployable to every hook target."""
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\n")
    hook = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": f"bash ./scripts/{name}.sh"}],
    }
    write(root / ".apm" / "hooks" / f"{name}.json", json.dumps({"hooks": {"PreToolUse": [hook]}}))
    write(root / ".apm" / "hooks" / "scripts" / f"{name}.sh", "#!/bin/bash\necho guard\n")


def _residue(world: ReplayWorld, root: str, script: str) -> list[str]:
    """Files under *root* that are, or name, the package script."""
    base = world.project / root
    return sorted(
        path.relative_to(world.project).as_posix()
        for path in base.rglob("*")
        if path.is_file() and (path.name == script or script in path.read_text(encoding="utf-8"))
    )


def _manifest(world: ReplayWorld, names: list[str], targets: tuple[str, ...] = ("claude",)) -> None:
    manifest = {
        "name": "proj",
        "version": "1.0.0",
        "dependencies": {"apm": [f"./vendor/{name}" for name in names]},
        "executables": {"allow": {f"./vendor/{name}": {"hooks": True} for name in names}},
    }
    if targets:
        manifest["targets"] = list(targets)
    dump_yaml(manifest, world.project / "apm.yml")


def _entries(world: ReplayWorld) -> dict[str, list[str]]:
    path = world.project / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
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
    result = world.apm("install")

    assert _entries(world) == {"PreToolUse": ["Edit"]}
    assert '"echo h"' in settings.read_text(encoding="utf-8")
    text = " ".join((result.stdout + result.stderr).split())
    assert "Kept an edited hook entry in .claude/settings.json" in text, text


def test_install_retires_the_hooks_in_a_declared_target_it_skips(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A run narrowed by --target still retires the entry in another declared runtime."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _script_hook_package(world.project / "vendor" / "h", "h")
    for root in (".claude", ".gemini"):
        (world.project / root).mkdir()
    _manifest(world, ["h"], ("claude", "gemini"))
    world.apm("install")
    assert _residue(world, ".gemini", "h.sh"), "the hook did not deploy to gemini"

    _manifest(world, [], ("claude", "gemini"))
    world.apm("install", "--target", "claude")

    assert _residue(world, ".gemini", "h.sh") == []
    gemini = world.project / ".gemini" / "settings.json"
    assert not gemini.exists() or "h.sh" not in gemini.read_text(encoding="utf-8")


def test_install_retires_the_hooks_in_a_flag_only_target_it_skips(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Without targets: in apm.yml, a runtime chosen only by --target earlier is still cleaned."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _script_hook_package(world.project / "vendor" / "h", "h")
    for root in (".claude", ".gemini"):
        (world.project / root).mkdir()
    _manifest(world, ["h"], ())
    world.apm("install", "--target", "claude,gemini")
    assert _residue(world, ".gemini", "h.sh"), "the hook did not deploy to gemini"

    _manifest(world, [], ())
    world.apm("install", "--target", "claude")

    assert _residue(world, ".gemini", "h.sh") == []
    gemini = world.project / ".gemini" / "settings.json"
    assert not gemini.exists() or "h.sh" not in gemini.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="needs an unprivileged symlink")
def test_install_never_rewrites_a_linked_hook_config(tmp_path: Path, apm_binary_path: Path) -> None:
    """A .claude directory replaced by a symlink is not followed; the row stays for a retry."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _hook_package(world.project / "vendor" / "h", "h", "Bash")
    (world.project / ".claude").mkdir()
    _manifest(world, ["h"])
    world.apm("install")
    outside = tmp_path / "outside-claude"
    (world.project / ".claude").rename(outside)
    os.symlink(outside, world.project / ".claude", target_is_directory=True)
    external = (outside / "settings.json").read_bytes()

    _manifest(world, [])
    world.apm("install", expect=None)

    assert (outside / "settings.json").read_bytes() == external
    assert "_local/h" in (outside / "apm-hooks.json").read_text(encoding="utf-8")


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs an unwritable directory"
)
@pytest.mark.parametrize("command", [("install",), ("install", "--update")])
def test_install_retries_a_hook_config_it_could_not_rewrite(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    """A failed rewrite keeps the lock row; the next install retires the entry."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _hook_package(world.project / "vendor" / "h", "h", "Bash")
    (world.project / ".claude").mkdir()
    _manifest(world, ["h"])
    world.apm("install")
    claude = world.project / ".claude"
    _manifest(world, [])
    claude.chmod(0o555)
    try:
        world.apm(*command, expect=None)
    finally:
        claude.chmod(0o755)
    assert "_local/h" in (world.project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert _entries(world) == {"PreToolUse": ["Bash"]}

    world.apm("install")

    assert _entries(world).get("PreToolUse", []) == []
    assert "_local/h" not in (world.project / "apm.lock.yaml").read_text(encoding="utf-8")
