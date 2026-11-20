"""APMLifecycle: a parked native plugin is approvable with the command APM prints (#4154).

PROMISE: outcome.status_matches_state -- the approve hint a parked install prints
reaches an approved, deployable state, and `apm approve` exits 1 when it
approves nothing (source: issue #4154).
CELL: OP=install,approve SRC=local PRIM=native-plugin(claude,codex)
SCOPE={user (APM_HOME outside HOME), project} GATE=executables.allow={}
owner: executable-trust-context (security/trust.py approve_command,
approval_scan_dir) + native-agent-plugin-registration-admission
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-LAW-outcome.status_matches_state",
        "P-OWN-executable-trust-context",
        "P-OWN-native-agent-plugin-registration-admission",
        "P-OWN-codex-native-plugin-lifecycle",
    ),
]

_SERVER = "apm-lifecycle-inert-server"


def _evidence(result: CommandResult) -> str:
    return f"argv={result.command!r} exit={result.returncode}\n{result.stdout}\n{result.stderr}"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _skill(root: Path, name: str) -> None:
    skill = root / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: {name}\n---\n{name}\n", encoding="ascii")


def _claude_plugin(root: Path) -> Path:
    plugin = root / "p-native-claude"
    _write_json(plugin / ".claude-plugin" / "plugin.json", {"name": "p-native-claude"})
    _skill(plugin, "p-native-claude")
    _write_json(plugin / ".mcp.json", {"mcpServers": {"p-claude-mcp": {"command": _SERVER}}})
    return plugin


def _codex_plugin(root: Path) -> Path:
    plugin = root / "p-native-codex"
    _write_json(
        plugin / ".codex-plugin" / "plugin.json",
        {
            "name": "p-native-codex",
            "version": "1.0.0",
            "skills": "./skills",
            "mcpServers": "./.mcp.codex.json",
        },
    )
    _skill(plugin, "p-native-codex")
    _write_json(plugin / ".mcp.codex.json", {"mcpServers": {"p-codex-mcp": {"command": _SERVER}}})
    return plugin


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, scope: str, target: str) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path.resolve() / "world", base_env=os.environ)
        self.scope = scope
        self.home = isolated.home
        self.apm_home = isolated.root / "apm-home"
        self.apm_home.mkdir()
        self.env = isolated.subprocess_env()
        self.env.update(APM_HOME=str(self.apm_home), APM_NO_CACHE="1", COLUMNS="400")
        for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            self.env.pop(variable, None)
        self.packages = isolated.package_root
        self.cwd = isolated.work_root
        self.project = isolated.work_root / "project"
        self.project.mkdir()
        manifest = {
            "name": scope,
            "version": "1.0.0",
            "targets": [target],
            "executables": {"allow": {}},
        }
        self.manifest.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    @property
    def manifest(self) -> Path:
        return (self.apm_home if self.scope == "user" else self.project) / "apm.yml"

    @property
    def flags(self) -> tuple[str, ...]:
        return ("-g",) if self.scope == "user" else ()

    def apm(self, *argv: str) -> CommandResult:
        cwd = self.cwd if self.scope == "user" else self.project
        result = self.runner.run(argv, cwd=cwd, env=self.env)
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
        return result

    def run_hint(self, output: str, pattern: str) -> CommandResult:
        """Execute the exact command APM printed (Step 9: every hint is a test)."""
        match = re.search(pattern, output)
        assert match, f"no hint matching {pattern!r} in:\n{output}"
        argv = shlex.split(match.group(1))
        assert argv[0] == "apm", argv
        return self.apm(*argv[1:])


def _parked_install(world: _World, plugin: Path) -> CommandResult:
    installed = world.apm("install", *world.flags, str(plugin))
    assert installed.returncode == 0, _evidence(installed)
    assert "Parked" in installed.stdout, _evidence(installed)
    return installed


def test_user_scope_claude_plugin_is_approved_by_the_printed_command(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, "user", "claude")
    plugin = _claude_plugin(world.packages)
    installed = _parked_install(world, plugin)
    settings = world.home / ".claude" / "settings.json"

    def enabled() -> bool:
        return settings.is_file() and "p-native-claude" in settings.read_text("utf-8")

    assert not enabled(), "a parked plugin was enabled in Claude settings"

    explained = world.run_hint(installed.stdout, r"Explain: (apm policy explain -g \S+)")
    assert explained.returncode == 0 and "verdict: blocked" in explained.stdout, _evidence(
        explained
    )
    approved = world.run_hint(installed.stdout, r"Approve: (apm approve -g \S+)")
    assert approved.returncode == 0, _evidence(approved)
    assert "Run 'apm install -g' to deploy" in approved.stdout, _evidence(approved)

    deployed = world.apm("install", "-g")
    assert deployed.returncode == 0, _evidence(deployed)
    assert "Parked" not in deployed.stdout, _evidence(deployed)
    assert enabled(), _evidence(deployed)
    pending = world.apm("approve", "-g", "--pending")
    assert "All packages with executables are approved." in pending.stdout, _evidence(pending)

    # I2: the converged install changes no bytes.
    before = ArtifactSnapshot.capture(world.home), ArtifactSnapshot.capture(world.apm_home)
    again = world.apm("install", "-g")
    assert again.returncode == 0, _evidence(again)
    assert_unchanged(before[0], ArtifactSnapshot.capture(world.home))
    assert_unchanged(before[1], ArtifactSnapshot.capture(world.apm_home))


def test_user_scope_codex_bundle_parks_and_the_printed_command_approves_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, "user", "codex")
    plugin = _codex_plugin(world.packages)
    installed = _parked_install(world, plugin)
    # Parked: nothing from the bundle is registered or projected loose.
    assert not (world.home / ".codex" / "config.toml").exists()
    assert not list((world.home / ".codex").rglob("SKILL.md"))

    approved = world.run_hint(installed.stdout, r"Re-approve: (apm approve -g \S+)")
    assert approved.returncode == 0, _evidence(approved)
    grants = yaml.safe_load(world.manifest.read_text("utf-8"))["executables"]["allow"]
    assert str(plugin) in grants, grants


@pytest.mark.parametrize("scope", ["user", "project"])
def test_approve_exits_1_when_it_approves_nothing(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _World(tmp_path, apm_binary_path, scope, "claude")
    plugin = _claude_plugin(world.packages)
    _parked_install(world, plugin)
    before = world.manifest.read_bytes()

    unknown = world.apm("approve", *world.flags, "nope/nope")
    assert unknown.returncode == 1, _evidence(unknown)
    assert "not found in installed packages" in unknown.stdout + unknown.stderr

    with_ref = world.apm("approve", *world.flags, f"{plugin}#v1.0.0")
    assert with_ref.returncode == 1, _evidence(with_ref)
    assert world.manifest.read_bytes() == before, "a refused approve wrote apm.yml"

    by_identity = world.run_hint(
        with_ref.stdout + with_ref.stderr, r"not a ref: (apm approve (?:-g )?\S+)"
    )
    assert by_identity.returncode == 0, _evidence(by_identity)
