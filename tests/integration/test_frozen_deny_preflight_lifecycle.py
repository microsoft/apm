"""APMLifecycle: apm install --frozen refuses a deny before any write (RA-024, #3957).

PROMISE: P-INV-I7 -- a failed command leaves durable state as it found it.
After ``apm deny`` withholds an installed hook package's executables, the lock
is stale. ``apm install --frozen`` may not rewrite it, so it refuses in its
preflight and keeps every deployed file and the lockfile byte for byte; the
plain ``apm install`` it names retires the hook, after which ``--frozen``
passes again. ``apm audit --ci`` agrees with ``--frozen`` on both sides (I3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
    pytest.mark.promise("P-INV-I3"),
]

_HOOK = Path(".github") / "hooks" / "p-hook-session.json"
_SESSION_HOOK = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo p-hook"}]}]}
}


def _hook_only_package(world: World) -> str:
    """A local package whose only executable is one hook (no MCP server, no bin)."""
    root = world.root / "authored" / "p-hook"
    (root / ".apm" / "hooks").mkdir(parents=True)
    (root / "apm.yml").write_text(
        yaml.safe_dump({"name": "p-hook", "version": "1.0.0"}), encoding="utf-8"
    )
    (root / ".apm" / "hooks" / "session.json").write_text(
        json.dumps(_SESSION_HOOK, indent=2) + "\n", encoding="ascii"
    )
    return root.as_posix()


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _ok(world: World, *args: str) -> CommandResult:
    result = _apm(world, *args)
    assert result.returncode == 0, _evidence(result)
    return result


def _state(world: World) -> dict[str, bytes]:
    root = world.project
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not {".git", "apm_modules", ".apm"} & set(path.relative_to(root).parts)
    }


def test_frozen_refuses_a_deny_before_any_write_and_install_retires(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["copilot"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    spec = _hook_only_package(world)
    _ok(world, "install", spec)
    _ok(world, "approve", spec)
    _ok(world, "install")
    assert (world.project / _HOOK).is_file(), "the approved hook must deploy"

    _ok(world, "deny", spec)
    before = _state(world)

    audit = _apm(world, "audit", "--ci")
    frozen = _apm(world, "install", "--frozen")

    assert frozen.returncode == 1, _evidence(frozen)
    text = " ".join(f"{frozen.stdout} {frozen.stderr}".split())
    assert "[x] --frozen" in text, _evidence(frozen)
    assert "apm install" in text, _evidence(frozen)
    assert _state(world) == before, "I7: refused --frozen changed durable state"
    assert audit.returncode != 0, "I3: audit --ci passed where --frozen refused\n" + _evidence(
        audit
    )
    assert "apm install" in " ".join(f"{audit.stdout} {audit.stderr}".split()), _evidence(audit)

    # The named remedy converges: install retires the hook, then --frozen agrees.
    _ok(world, "install")
    assert not (world.project / _HOOK).exists()
    _ok(world, "install", "--frozen")
    _ok(world, "audit", "--ci")
