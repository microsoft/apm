"""APMLifecycle: a native Claude plugin that is no longer allowed is retired (#4444-#4446).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is deployed.
Once a gate change stops allowing the approved local native Claude plugin
``p-native-claude``, the next mutating command removes its native surface (its
deployed root under ``.agents/plugins/`` and its ``enabledPlugins`` entry in
``.claude/settings.json``, #3921) or refuses, as install does:

- #4446: uninstalling another package rebuilds the integration and retires it,
  for a changed definition, a local deny, an org deny, a revoked approval, a
  policy dependency deny and an org MCP policy that blocks its server (#3921);
- #4445: ``apm update`` under an org policy that denies the plugin exits 1 and
  names it;
- #4444: ``apm update`` after the plugin's definition changed since approval
  retires it.
- #4754: ``apm prune --dry-run`` after the approval is revoked names the root
  and settings declaration a real prune removes, and changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
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


def _native_surface(world: World, plugin: str = _PLUGIN) -> bool:
    """Whether Claude would load the plugin: its deployed root, or APM's true for it."""
    if (world.project / ".agents/plugins" / plugin).exists():
        return True
    settings = world.project / ".claude/settings.json"
    if not settings.is_file():
        return False
    enabled = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins") or {}
    return any(key.startswith(f"{plugin}@") and value is True for key, value in enabled.items())


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _spec(world: World, key: str) -> str:
    return world.packages[key].spec


def _arrange(world: World, plugin: str = _PLUGIN, *flags: str) -> None:
    """Install the sibling and the approved native plugin for the claude target."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["claude"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    for args in (
        ("install", _spec(world, plugin), *flags),
        ("install", _spec(world, _SIBLING), *flags),
        ("approve", _spec(world, plugin)),
        ("install", *flags),
    ):
        result = _apm(world, *args)
        assert result.returncode == 0, _evidence(result)
    assert _native_surface(world, plugin)


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
    elif change == "policy-mcp-deny":
        use_policy(world, "mcp-deny")
    else:
        raise ValueError(change)


@pytest.mark.parametrize(
    "change",
    (
        "definition-changed",
        "local-deny",
        "org-deny",
        "revoke-approval",
        "policy-dep-deny",
        "policy-mcp-deny",
    ),
)
def test_uninstalling_another_package_retires_a_disallowed_native_plugin(
    lifecycle_world: World,  # noqa: F811
    change: str,
) -> None:
    """#4446: the rebuild after uninstalling p-skill removes the native surface."""
    _arrange(lifecycle_world)
    _change(lifecycle_world, change)

    result = _apm(lifecycle_world, "uninstall", _spec(lifecycle_world, _SIBLING))

    assert not _native_surface(lifecycle_world), _evidence(result)
    assert not (lifecycle_world.project / ".claude" / "skills" / _SIBLING).exists()


@pytest.mark.parametrize("update_args", (("update",), ("update", "--yes")), ids=("prompt", "yes"))
def test_update_refuses_a_native_plugin_the_org_policy_denies(
    lifecycle_world: World,  # noqa: F811
    update_args: tuple[str, ...],
) -> None:
    """#4445: apm update exits 1, names the denied plugin and the policy reason."""
    _arrange(lifecycle_world)
    use_policy(lifecycle_world, "deny-native")

    result = _apm(lifecycle_world, *update_args)

    assert result.returncode == 1, _evidence(result)
    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert _PLUGIN in output, _evidence(result)
    assert "denied by pattern" in output, _evidence(result)
    assert _native_surface(lifecycle_world), "a refused update keeps the deployed state"


def _project_bytes(world: World) -> dict[str, bytes]:
    return {
        path.relative_to(world.project).as_posix(): path.read_bytes()
        for path in sorted(world.project.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_prune_dry_run_previews_a_native_plugin_the_gate_would_retire(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """#4754: prune --dry-run names the root and declarations prune would remove."""
    _arrange(lifecycle_world)
    _revoke(lifecycle_world)
    before = _project_bytes(lifecycle_world)

    preview = _apm(lifecycle_world, "prune", "--dry-run")

    assert preview.returncode == 0, _evidence(preview)
    assert _project_bytes(lifecycle_world) == before, "a dry run changes nothing"
    output = " ".join(preview.stdout.split())
    assert f"[dry-run] remove native plugin {_PLUGIN}" in output, _evidence(preview)
    assert f".agents/plugins/{_PLUGIN}" in output, _evidence(preview)
    assert ".claude/settings.json" in output, _evidence(preview)
    assert "Nothing to prune" not in output, _evidence(preview)

    result = _apm(lifecycle_world, "prune")

    assert result.returncode == 0, _evidence(result)
    assert not _native_surface(lifecycle_world), _evidence(result)


def test_update_retires_a_native_plugin_whose_definition_changed(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """#4444: apm update removes the native surface after a definition change."""
    _arrange(lifecycle_world)
    _redefine(lifecycle_world)

    result = _apm(lifecycle_world, "update", "--yes")

    assert not _native_surface(lifecycle_world), _evidence(result)
    assert result.returncode == 0, _evidence(result)
