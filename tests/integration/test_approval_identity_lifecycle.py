"""APMLifecycle: approve, deny and policy explain resolve install specs (#4189, #4245, #4246).

PROMISE: outcome.status_matches_state -- every spec form approve, deny and policy
explain accept names the installed package's store identity, or the command
exits 1 without writing; a deny by install spec retires deployed executables
(source: issues #4189, #4245, #4246). A deny an older APM keyed by the raw
spec is honored after upgrade and stays honored through update and --frozen.
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
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-LAW-outcome.status_matches_state",
        "P-OWN-executable-trust-context",
        "P-OWN-dependency-cli-selection",
    ),
]

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


@pytest.mark.parametrize("spec", ["absent@loc", "acme/absent"])
@pytest.mark.parametrize("command", ["approve", "deny", "explain"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_unresolvable_install_spec_exits_1_without_writing(
    tmp_path: Path, apm_binary_path: Path, scope: str, command: str, spec: str
) -> None:
    world = _installed(tmp_path, apm_binary_path, scope)
    before = world.manifest.read_bytes()

    argv = ("policy", "explain") if command == "explain" else (command,)
    result = world.apm(*argv, *world.flags, spec)

    assert result.returncode == 1, _evidence(result)
    assert spec in result.stdout + result.stderr
    assert "Denied" not in result.stdout, _evidence(result)
    assert world.manifest.read_bytes() == before, "a refused spec wrote the approval store"


def _snapshots(world: _World) -> tuple[ArtifactSnapshot, ArtifactSnapshot]:
    deploy = world.home if world.scope == "user" else world.project
    return ArtifactSnapshot.capture(deploy / ".claude"), ArtifactSnapshot.capture(
        world.manifest.parent
    )


def _approved_and_deployed(tmp_path: Path, apm_binary_path: Path, scope: str) -> _World:
    world = _installed(tmp_path, apm_binary_path, scope)
    approved = world.apm("approve", *world.flags, _SPEC)
    assert approved.returncode == 0, _evidence(approved)
    deployed = world.apm("install", *world.flags)
    assert deployed.returncode == 0 and _deployed(world), _evidence(deployed)
    return world


def _assert_retired_and_converged(world: _World) -> None:
    """The hook stays retired through install, update and --frozen (I2, I4)."""
    retired = world.apm("install", *world.flags)
    assert retired.returncode == 0, _evidence(retired)
    assert not _deployed(world), _evidence(retired)
    settings = _settings(world)
    assert not settings.is_file() or _MARKER not in settings.read_text(encoding="utf-8")
    before = _snapshots(world)
    for argv in (("update", "--yes", *world.flags), ("install", "--frozen", *world.flags)):
        result = world.apm(*argv)
        assert result.returncode == 0, _evidence(result)
        assert not _deployed(world), _evidence(result)
        after = _snapshots(world)
        assert_unchanged(before[0], after[0])
        assert_unchanged(before[1], after[1])


@pytest.mark.parametrize("scope", ["project", "user"])
def test_deny_by_install_spec_stays_retired_through_update_and_frozen(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _approved_and_deployed(tmp_path, apm_binary_path, scope)
    hooked = _snapshots(world)

    denied = world.apm("deny", *world.flags, _SPEC)
    assert denied.returncode == 0, _evidence(denied)
    _assert_retired_and_converged(world)
    # The snapshot records the removal: the hook config changed.
    assert _snapshots(world)[0] != hooked[0]


@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_spec_deny_from_an_older_apm_is_honored_after_upgrade(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    """History: a deny an older APM keyed by the raw install spec still retires the hook."""
    world = _approved_and_deployed(tmp_path, apm_binary_path, scope)
    manifest = yaml.safe_load(world.manifest.read_text(encoding="utf-8"))
    types = {"hooks": True, "mcp": True, "lsp": True, "bin": True, "canvas": True}
    manifest["executables"]["deny"] = {_SPEC: types}
    world.manifest.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    upgraded = world.apm("install", *world.flags)
    assert upgraded.returncode == 0, _evidence(upgraded)
    assert not _deployed(world), _evidence(upgraded)
    notes = [line for line in upgraded.stdout.splitlines() if "names an install spec" in line]
    assert len(notes) == 1, _evidence(upgraded)
    _assert_retired_and_converged(world)

    # The next store write records the deny under the package identity.
    rewritten = world.apm("deny", *world.flags, _SPEC)
    assert rewritten.returncode == 0, _evidence(rewritten)
    assert _SPEC not in _store(world)["deny"], _store(world)
