"""APMLifecycle: approve, deny and policy explain resolve install specs (#4189, #4245, #4246).

PROMISE: outcome.status_matches_state -- every spec form approve, deny and policy
explain accept names the installed package's store identity, or the command
exits 1 without writing; a deny by install spec retires deployed executables
(source: issues #4189, #4245, #4246).
CELL: OP=approve,deny,explain,install SRC=marketplace(local catalog) PRIM=hook
SCOPE={project, user (APM_HOME outside HOME)} GATE=executables.allow={}
owner: executable-trust-context (security/executables.py canonical_approval_spec)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.integration.test_approve_parked_native_plugin_lifecycle import _evidence, _World

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_MARKER = "hooky-ran"
_SPEC = "hooky@loc"


def _catalog(world: _World) -> Path:
    catalog = world.packages / "catalog"
    plugin = catalog / "plugins" / "hooky"
    hook = plugin / ".apm" / "hooks" / "lint.json"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Write", "hooks": [{"type": "command", "command": _MARKER}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (plugin / "apm.yml").write_text("name: hooky\nversion: 1.0.0\n", encoding="utf-8")
    (catalog / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "loc",
                "owner": {"name": "APM Test"},
                "plugins": [{"name": "hooky", "source": "./plugins/hooky", "version": "1.0.0"}],
            }
        ),
        encoding="utf-8",
    )
    return catalog


def _settings(world: _World) -> Path:
    root = world.home if world.scope == "user" else world.project
    return root / ".claude" / "settings.json"


def _deployed(world: _World) -> bool:
    settings = _settings(world)
    return settings.is_file() and _MARKER in settings.read_text(encoding="utf-8")


def _store(world: _World) -> dict:
    return yaml.safe_load(world.manifest.read_text(encoding="utf-8")).get("executables") or {}


def _installed(tmp_path: Path, apm_binary_path: Path, scope: str) -> _World:
    world = _World(tmp_path, apm_binary_path, scope, "claude")
    added = world.apm("marketplace", "add", str(_catalog(world)), "--name", "loc")
    assert added.returncode == 0, _evidence(added)
    installed = world.apm("install", *world.flags, _SPEC)
    assert installed.returncode == 0, _evidence(installed)
    assert not _deployed(world), "the gate deployed an unapproved hook"
    return world


@pytest.mark.parametrize("scope", ["project", "user"])
def test_approve_and_deny_by_install_spec_reach_the_installed_package(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _installed(tmp_path, apm_binary_path, scope)

    # #4245: the install spec approves the installed package.
    approved = world.apm("approve", *world.flags, _SPEC)
    assert approved.returncode == 0, _evidence(approved)
    assert _SPEC not in _store(world).get("allow", {})
    deployed = world.apm("install", *world.flags)
    assert deployed.returncode == 0, _evidence(deployed)
    assert _deployed(world), _evidence(deployed)

    # #4189: the install spec denies the same package, and install retires the hook.
    denied = world.apm("deny", *world.flags, _SPEC)
    assert denied.returncode == 0, _evidence(denied)
    assert _SPEC not in _store(world).get("deny", {}), _store(world)
    retired = world.apm("install", *world.flags)
    assert retired.returncode == 0, _evidence(retired)
    assert not _deployed(world), _evidence(retired)

    # #4246: explain names the deny and how to lift it, never "To trust it".
    explained = world.apm("policy", "explain", *world.flags, _SPEC)
    assert explained.returncode == 0, _evidence(explained)
    assert "To trust it" not in explained.stdout, _evidence(explained)
    assert "To lift it" in explained.stdout, _evidence(explained)


@pytest.mark.parametrize("command", ["approve", "deny"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_unresolvable_install_spec_exits_1_without_writing(
    tmp_path: Path, apm_binary_path: Path, scope: str, command: str
) -> None:
    world = _installed(tmp_path, apm_binary_path, scope)
    before = world.manifest.read_bytes()

    result = world.apm(command, *world.flags, "absent@loc")

    assert result.returncode == 1, _evidence(result)
    assert "absent@loc" in result.stdout + result.stderr
    assert world.manifest.read_bytes() == before, "a refused spec wrote the approval store"
