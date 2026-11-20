"""APMLifecycle: a native Claude plugin that is no longer allowed is retired (#4444-#4446).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is deployed.
Once a gate change stops allowing the approved local native Claude plugin
``p-native-claude``, the next mutating command removes its native surface (the
plugin's MCP server in the project ``.mcp.json``) or refuses, as install does:

- #4446: uninstalling another package rebuilds the integration and retires it,
  for a changed definition, a local deny, an org deny, a revoked approval and a
  policy dependency deny;
- #4445: ``apm update`` under an org policy that denies the plugin exits 1 and
  names it;
- #4444: ``apm update`` after the plugin's definition changed since approval
  retires it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.fixtures import lifecycle_template, lifecycle_world  # noqa: F401
from tests.lifecycle.world.services import ORG
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4"),
]

_PLUGIN = "p-native-claude"
_SIBLING = "p-skill"
_SERVER = f'"{_PLUGIN}-mcp"'


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _spec(world: World, key: str) -> str:
    return world.packages[key].spec


def _arrange(world: World) -> None:
    """Install the sibling and the approved native plugin for the claude target."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["claude"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    for args in (
        ("install", _spec(world, _PLUGIN)),
        ("install", _spec(world, _SIBLING)),
        ("approve", _spec(world, _PLUGIN)),
        ("install",),
    ):
        result = _apm(world, *args)
        assert result.returncode == 0, _evidence(result)
    assert _SERVER in (world.project / ".mcp.json").read_text(encoding="utf-8")


def _redefine(world: World) -> None:
    """Publish a new definition of the plugin's MCP server (an added argument)."""
    config = Path(_spec(world, _PLUGIN)) / ".mcp.json"
    data = json.loads(config.read_text(encoding="ascii"))
    for server in data["mcpServers"].values():
        server["args"] = [*server.get("args", []), "--v2"]
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")


def _revoke(world: World) -> None:
    """Drop every approval-store entry naming the plugin from apm.yml."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    identities = (_spec(world, _PLUGIN), _PLUGIN, f"{ORG}/{_PLUGIN}")
    for store in ("allow", "deny"):
        entries = (data.get("executables") or {}).get(store) or {}
        for entry in list(entries):
            if any(entry == i or entry.startswith(f"{i}#") for i in identities):
                del entries[entry]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _change(world: World, change: str) -> None:
    if change == "definition-changed":
        _redefine(world)
    elif change == "local-deny":
        result = _apm(world, "deny", _spec(world, _PLUGIN))
        assert result.returncode == 0, _evidence(result)
    elif change == "org-deny":
        use_policy(world, "exec-deny-all")
    elif change == "revoke-approval":
        _revoke(world)
    elif change == "policy-dep-deny":
        use_policy(world, "deny-native")
    else:
        raise ValueError(change)


@pytest.mark.parametrize(
    "change",
    ("definition-changed", "local-deny", "org-deny", "revoke-approval", "policy-dep-deny"),
)
def test_uninstalling_another_package_retires_a_disallowed_native_plugin(
    lifecycle_world: World,  # noqa: F811
    change: str,
) -> None:
    """#4446: the rebuild after uninstalling p-skill removes the native surface."""
    _arrange(lifecycle_world)
    _change(lifecycle_world, change)

    result = _apm(lifecycle_world, "uninstall", _spec(lifecycle_world, _SIBLING))

    config = lifecycle_world.project / ".mcp.json"
    assert _SERVER not in (config.read_text(encoding="utf-8") if config.is_file() else ""), (
        _evidence(result)
    )
    assert not (lifecycle_world.project / ".claude" / "skills" / _SIBLING).exists()


def test_update_refuses_a_native_plugin_the_org_policy_denies(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """#4445: apm update exits 1 and names the denied plugin, as install does."""
    _arrange(lifecycle_world)
    use_policy(lifecycle_world, "deny-native")

    result = _apm(lifecycle_world, "update", "--yes")

    assert result.returncode == 1, _evidence(result)
    assert _PLUGIN in f"{result.stdout}\n{result.stderr}", _evidence(result)


def test_update_retires_a_native_plugin_whose_definition_changed(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """#4444: apm update removes the native surface after a definition change."""
    _arrange(lifecycle_world)
    _redefine(lifecycle_world)

    result = _apm(lifecycle_world, "update", "--yes")

    config = lifecycle_world.project / ".mcp.json"
    assert _SERVER not in (config.read_text(encoding="utf-8") if config.is_file() else ""), (
        _evidence(result)
    )
    assert result.returncode == 0, _evidence(result)
