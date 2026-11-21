"""APMLifecycle: deny retirement is the same with or without apm.lock.yaml.

PROMISE: P-INV-I4 -- a gate decision is monotonic: once a package's
executables are withheld, install retires what APM deployed for them.
A project whose lockfile was lost (not committed, deleted, regenerated)
converges exactly like one that kept it, and ``apm audit --ci`` agrees.
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
    pytest.mark.promise("P-INV-I4"),
    pytest.mark.promise("P-INV-I3"),
]

_HOOK = Path(".github") / "hooks" / "p-gate-session.json"
_SERVER = "p-gate-mcp"
_SESSION_HOOK = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo p-gate"}]}]}
}


def _package(world: World) -> str:
    """A local package with one hook and one self-defined stdio MCP server."""
    root = world.root / "authored" / "p-gate"
    (root / ".apm" / "hooks").mkdir(parents=True)
    manifest = {
        "name": "p-gate",
        "version": "1.0.0",
        "dependencies": {
            "mcp": [{"name": _SERVER, "registry": False, "transport": "stdio", "command": "p-gate"}]
        },
    }
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
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


def _servers(world: World) -> set[str]:
    path = world.project / ".mcp.json"
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {})


def _deploy_approved(world: World) -> str:
    """Install, approve and deploy the package's hook and MCP server for copilot."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["copilot"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    spec = _package(world)
    _ok(world, "install", spec, "--trust-transitive-mcp")
    _ok(world, "approve", spec)
    _ok(world, "install", "--trust-transitive-mcp")
    assert (world.project / _HOOK).is_file(), "the approved hook must deploy"
    assert _SERVER in _servers(world), "the approved MCP server must deploy"
    return spec


@pytest.mark.parametrize("lock", ["kept", "lost"])
def test_deny_retires_hook_and_mcp_with_or_without_the_lockfile(
    lifecycle_world: World,  # noqa: F811
    lock: str,
) -> None:
    world = lifecycle_world
    spec = _deploy_approved(world)

    _ok(world, "deny", spec)
    if lock == "lost":
        (world.project / "apm.lock.yaml").unlink()

    _ok(world, "install", "--trust-transitive-mcp")

    assert not (world.project / _HOOK).exists(), "a denied hook stays deployed"
    assert _SERVER not in _servers(world), "a denied MCP server stays deployed"
    _ok(world, "install", "--trust-transitive-mcp")
    assert not (world.project / _HOOK).exists()
    _ok(world, "audit", "--ci")


@pytest.mark.parametrize("lock", ["kept", "lost"])
def test_deny_keeps_an_edited_hook_and_audit_names_it_with_or_without_the_lockfile(
    lifecycle_world: World,  # noqa: F811
    lock: str,
) -> None:
    """Edited bytes are the user's: install keeps them, and audit --ci reports the file."""
    world = lifecycle_world
    spec = _deploy_approved(world)
    hook = world.project / _HOOK
    edited = hook.read_text(encoding="utf-8").replace("echo p-gate", "echo edited")
    hook.write_text(edited, encoding="utf-8")

    _ok(world, "deny", spec)
    if lock == "lost":
        (world.project / "apm.lock.yaml").unlink()
    _ok(world, "install", "--trust-transitive-mcp")

    assert hook.read_text(encoding="utf-8") == edited, "install must keep edited bytes"
    audit = _apm(world, "audit", "--ci")
    assert audit.returncode != 0, _evidence(audit)
    assert _HOOK.as_posix() in audit.stdout + audit.stderr, _evidence(audit)
