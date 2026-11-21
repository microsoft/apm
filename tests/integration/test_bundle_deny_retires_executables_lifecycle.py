"""APMLifecycle: a deny retires every executable of an installed local bundle (RA-025, #3956).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is deployed.
After the deny, ``apm audit --ci`` flags the stale bundle and names the
remedy; once ``apm install`` retires it, ``install --frozen`` and
``audit --ci`` both pass (I3).
"""

from __future__ import annotations

import re

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

_BUNDLE = "p-exec:bundle"
_SERVER = "p-exec-mcp"


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


def _executables(world: World) -> dict[str, list[str]]:
    """Deployed hook files and the files that configure the bundle's MCP server."""
    root = world.project
    found: dict[str, list[str]] = {"hook": [], "mcp": []}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or {"apm_modules", ".git", ".apm"} & set(rel.parts):
            continue
        if rel.name in ("apm.yml", "apm.lock.yaml"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "echo p-exec" in text and "hooks" in rel.as_posix():
            found["hook"].append(rel.as_posix())
        if _SERVER in text:
            found["mcp"].append(rel.as_posix())
    return found


def test_deny_then_install_retires_every_bundle_executable(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    spec = world.packages[_BUNDLE].spec
    first = _apm(world, "install", spec)
    key = re.search(r'"(p-exec#[^"]+@sha256:[0-9a-f]+)"', first.stdout)
    assert key, _evidence(first)
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("executables", {}).setdefault("allow", {})[key.group(1)] = {
        "hooks": True,
        "mcp": True,
        "bin": True,
    }
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    _ok(world, "install", spec)
    deployed = _executables(world)
    assert deployed["hook"] and deployed["mcp"], deployed

    denied = _ok(world, "deny", key.group(1))
    # The deny names the command that retires the bundle's executables; run it.
    hint = " ".join(f"{denied.stdout} {denied.stderr}".split())
    assert "apm install" in hint, _evidence(denied)
    stale = _apm(world, "audit", "--ci")
    assert stale.returncode != 0, "I3: audit --ci passed a denied bundle\n" + _evidence(stale)
    assert "apm install" in " ".join(f"{stale.stdout} {stale.stderr}".split()), _evidence(stale)
    _ok(world, "install")

    assert _executables(world) == {"hook": [], "mcp": []}, _evidence(denied)
    _ok(world, "install", "--frozen")
    _ok(world, "audit", "--ci")
