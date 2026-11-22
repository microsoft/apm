"""APMLifecycle: a parked native plugin is approvable with the command APM prints (#4154).

PROMISE: outcome.status_matches_state -- the approve hint a parked install prints
reaches an approved, deployable state, and `apm approve` exits 1 when it
approves nothing (source: issue #4154).
CELL: OP=install,approve SRC=local PRIM=native-plugin(claude),split-plugin(codex)
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
from tests.utils.codex_spy import write_codex_spy
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-LAW-outcome.status_matches_state",
        "P-OWN-executable-trust-context",
        "P-OWN-native-agent-plugin-registration-admission",
        "P-OWN-codex-split-install",
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


def _lock(world: _World) -> dict:
    return yaml.safe_load((world.manifest.parent / "apm.lock.yaml").read_text("utf-8"))


def _row(world: _World, plugin: Path) -> dict:
    (row,) = [r for r in _lock(world)["dependencies"] if r.get("local_path") == str(plugin)]
    return row


def _claude_deployed(world: _World, plugin: Path) -> bool:
    """The plugin is declared where Claude reads it, its deployed root carries its
    own MCP server, and the lock agrees (I1). Both scopes install it whole (#3921)."""
    base = world.home if world.scope == "user" else world.project
    settings = base / ".claude" / "settings.json"
    enabled = settings.is_file() and "p-native-claude" in settings.read_text("utf-8")
    row = _row(world, plugin)
    root = row.get("native_plugin") and base / row["native_plugin"]["root"]
    assert bool(enabled) == bool(root and (root / ".mcp.json").is_file()), row
    # Claude loads the server from the plugin; APM never claims it for Claude.
    claimed = (_lock(world).get("mcp_target_servers") or {}).get("claude") or []
    assert "p-claude-mcp" not in claimed, _lock(world)
    return bool(enabled)


@pytest.mark.parametrize("scope", ["user", "project"])
def test_claude_plugin_is_approved_by_the_printed_command(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _World(tmp_path, apm_binary_path, scope, "claude")
    plugin = _claude_plugin(world.packages)
    flag = r"-g " if scope == "user" else ""
    installed = _parked_install(world, plugin)
    assert not _claude_deployed(world, plugin), _evidence(installed)

    explained = world.run_hint(installed.stdout, rf"Explain: (apm policy explain {flag}\S+)")
    assert explained.returncode == 0, _evidence(explained)
    assert "verdict: blocked" in explained.stdout, _evidence(explained)
    approved = world.run_hint(installed.stdout, rf"Approve: (apm approve {flag}\S+)")
    assert approved.returncode == 0, _evidence(approved)

    # The next step approve prints names this scope's install, and running it deploys.
    install = "apm install -g" if scope == "user" else "apm install"
    deployed = world.run_hint(approved.stdout, rf"Run '({re.escape(install)})' to deploy")
    assert deployed.returncode == 0, _evidence(deployed)
    assert "Parked" not in deployed.stdout, _evidence(deployed)
    assert _claude_deployed(world, plugin), _evidence(deployed)
    pending = world.apm("approve", *world.flags, "--pending")
    assert "All packages with executables are approved." in pending.stdout, _evidence(pending)

    # I2: the converged install changes no bytes.
    roots = (world.home, world.apm_home, world.project)
    before = [ArtifactSnapshot.capture(root) for root in roots]
    again = world.apm("install", *world.flags)
    assert again.returncode == 0, _evidence(again)
    for root, snapshot in zip(roots, before, strict=True):
        assert_unchanged(snapshot, ArtifactSnapshot.capture(root))

    # I4: denying the same identity retires the plugin on the next install.
    denied = world.apm("deny", *world.flags, str(plugin))
    assert denied.returncode == 0, _evidence(denied)
    retired = world.run_hint(
        denied.stdout, rf"Run '({re.escape(install)})' to remove executables already deployed"
    )
    assert retired.returncode == 0, _evidence(retired)
    assert not _claude_deployed(world, plugin), _evidence(retired)


def test_a_narrowed_run_refuses_to_carry_a_denied_claude_plugin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """--target copilot never reaches a user Claude plugin, so it cannot keep it once denied."""
    world = _World(tmp_path, apm_binary_path, "user", "claude")
    plugin = _claude_plugin(world.packages)
    installed = _parked_install(world, plugin)
    approved = world.run_hint(installed.stdout, r"Approve: (apm approve -g \S+)")
    assert approved.returncode == 0, _evidence(approved)
    deployed = world.apm("install", "-g")
    assert deployed.returncode == 0, _evidence(deployed)
    assert _claude_deployed(world, plugin), _evidence(deployed)
    denied = world.apm("deny", "-g", str(plugin))
    assert denied.returncode == 0, _evidence(denied)

    narrowed = world.apm("install", "-g", "--target", "copilot")
    assert narrowed.returncode == 1, _evidence(narrowed)
    assert "is no longer allowed" in narrowed.stdout + narrowed.stderr, _evidence(narrowed)
    assert _claude_deployed(world, plugin), _evidence(narrowed)

    retired = world.apm("install", "-g")
    assert retired.returncode == 0, _evidence(retired)
    assert not _claude_deployed(world, plugin), _evidence(retired)


def _codex_world(tmp_path: Path, apm_binary_path: Path) -> tuple[_World, Path, Path]:
    world = _World(tmp_path, apm_binary_path, "user", "codex")
    marker = world.packages.parent / "codex-ran"
    spy = write_codex_spy(world.packages.parent / "codex-bin", marker)
    world.env["PATH"] = os.pathsep.join((str(spy), world.env["PATH"]))
    return world, _codex_plugin(world.packages), marker


def _codex_mcp_live(world: _World, plugin: Path) -> bool:
    """The plugin's MCP server is in Codex's config, and the lock agrees (I1)."""
    config = world.home / ".codex" / "config.toml"
    live = config.is_file() and "p-codex-mcp" in config.read_text("utf-8")
    assert live == ("p-codex-mcp" in (_lock(world).get("mcp_servers") or [])), _lock(world)
    return live


def _codex_skill(world: _World) -> Path:
    return world.home / ".agents" / "skills" / "p-native-codex" / "SKILL.md"


def test_codex_bundle_mcp_waits_for_the_printed_command_while_skills_split_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A Codex bundle is split-installed (#3920); its MCP waits for approval (#4154)."""
    world, plugin, marker = _codex_world(tmp_path, apm_binary_path)
    installed = world.apm("install", "-g", str(plugin))

    # Never silent: exit 0, the skill lands, the MCP server waits for the printed command.
    assert installed.returncode == 0, _evidence(installed)
    flat = " ".join(installed.stdout.split())
    assert "Codex: split-installed 1 plugin(s) at user scope (p-native-codex)" in flat, flat
    assert _codex_skill(world).is_file(), _evidence(installed)
    assert not _codex_mcp_live(world, plugin), _evidence(installed)
    row = _row(world, plugin)
    assert row["exec_status"] == "gated_pending_approval", row

    approved = world.run_hint(installed.stdout, r"(apm approve -g \S+)")
    assert approved.returncode == 0, _evidence(approved)
    deployed = world.apm("install", "-g")
    assert deployed.returncode == 0, _evidence(deployed)
    assert _codex_mcp_live(world, plugin), _evidence(deployed)
    replay = world.apm("install", "-g", "--frozen")
    assert replay.returncode == 0, _evidence(replay)
    audit = world.runner.run(("audit", "--ci"), cwd=world.apm_home, env=world.env)
    assert audit.returncode == 0, _evidence(audit)

    denied = world.apm("deny", "-g", str(plugin))
    assert denied.returncode == 0, _evidence(denied)
    retired = world.apm("install", "-g")
    assert retired.returncode == 0, _evidence(retired)
    assert not _codex_mcp_live(world, plugin), _evidence(retired)
    assert not marker.exists(), "APM must never run the codex binary"


def test_pending_codex_bundle_record_survives_and_uninstall_clears_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, plugin, marker = _codex_world(tmp_path, apm_binary_path)
    assert world.apm("install", "-g", str(plugin)).returncode == 0
    # A new process still sees the pending record.
    pending = world.apm("approve", "-g", "--pending")
    assert str(plugin) in pending.stdout, _evidence(pending)
    assert _row(world, plugin)["exec_status"] == "gated_pending_approval"

    removed = world.apm("uninstall", "-g", str(plugin))
    assert removed.returncode == 0, _evidence(removed)
    lock_path = world.manifest.parent / "apm.lock.yaml"
    rows = _lock(world).get("dependencies") or [] if lock_path.is_file() else []
    assert not any(r.get("local_path") == str(plugin) for r in rows)
    assert not (world.apm_home / "apm_modules" / "_local" / "p-native-codex").exists()
    assert not _codex_skill(world).exists()
    gone = world.apm("approve", "-g", str(plugin))
    assert gone.returncode == 1, _evidence(gone)
    assert not marker.exists(), "APM must never run the codex binary"


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
