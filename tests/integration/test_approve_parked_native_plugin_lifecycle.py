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
from tests.utils.codex_stand_in import write_codex_stand_in
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


def _lock(world: _World) -> dict:
    return yaml.safe_load((world.manifest.parent / "apm.lock.yaml").read_text("utf-8"))


def _row(world: _World, plugin: Path) -> dict:
    (row,) = [r for r in _lock(world)["dependencies"] if r.get("local_path") == str(plugin)]
    return row


def _claude_deployed(world: _World, plugin: Path) -> bool:
    """The plugin's MCP server is live where Claude reads it, and the lock agrees (I1)."""
    if world.scope == "user":
        settings = world.home / ".claude" / "settings.json"
        enabled = settings.is_file() and "p-native-claude" in settings.read_text("utf-8")
        row = _row(world, plugin)
        root = row.get("native_plugin") and world.home / row["native_plugin"]["root"]
        assert bool(enabled) == bool(root and (root / ".mcp.json").is_file()), row
        return bool(enabled)
    config = world.project / ".mcp.json"
    on_disk = config.is_file() and "p-claude-mcp" in config.read_text("utf-8")
    assert on_disk == ("p-claude-mcp" in (_lock(world).get("mcp_servers") or [])), _lock(world)
    return on_disk


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

    deployed = world.apm("install", *world.flags)
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
    retired = world.apm("install", *world.flags)
    assert retired.returncode == 0, _evidence(retired)
    assert not _claude_deployed(world, plugin), _evidence(retired)


def _codex_world(tmp_path: Path, apm_binary_path: Path) -> tuple[_World, Path]:
    world = _World(tmp_path, apm_binary_path, "user", "codex")
    stand_in = write_codex_stand_in(world.packages.parent / "codex-bin")
    world.env["PATH"] = os.pathsep.join((str(stand_in.parent), world.env["PATH"]))
    return world, _codex_plugin(world.packages)


def _codex_registered(world: _World, plugin: Path) -> bool:
    """The plugin is registered with Codex, and the lock and ownership ledger agree (I1)."""
    config = world.home / ".codex" / "config.toml"
    text = config.read_text("utf-8") if config.is_file() else ""
    registered = 'plugins."p-native-codex@' in text
    ledger = json.loads((world.apm_home / ".codex-native" / "ownership.json").read_text("utf-8"))
    record = ledger["plugins"].get(str(plugin))
    assert registered == bool(record), (text, ledger)
    assert registered == (_row(world, plugin)["exec_status"] == "deployed"), _row(world, plugin)
    if record:
        assert Path(record["installed_root"], ".codex-plugin", "plugin.json").is_file(), record
    return registered


def test_codex_bundle_parks_until_the_printed_command_approves_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, plugin = _codex_world(tmp_path, apm_binary_path)
    installed = world.apm("install", "-g", str(plugin))

    # Parked, never silent: exit 0, one notice naming the package and the exact command.
    assert installed.returncode == 0, _evidence(installed)
    lines = installed.stdout.splitlines()
    notice = f"[!] [{plugin}] Parked native plugin {plugin}: not approved"
    assert [line.strip() for line in lines].count(notice) == 1, _evidence(installed)
    hint = f"Review: apm approve -g --pending  |  Re-approve: apm approve -g {plugin}"
    assert hint in [line.strip() for line in lines], _evidence(installed)
    assert not any(line.startswith("[*] Installed") for line in lines), _evidence(installed)
    assert not (world.home / ".codex" / "config.toml").exists()
    row = _row(world, plugin)
    assert row["exec_status"] == "gated_pending_approval" and not row.get("deployed_files"), row

    # While parked, --frozen and audit --ci fail and name the same command.
    frozen = world.apm("install", "-g", "--frozen")
    assert frozen.returncode == 1, _evidence(frozen)
    assert f"apm approve -g {plugin}" in frozen.stdout + frozen.stderr, _evidence(frozen)
    audit = world.runner.run(("audit", "--ci"), cwd=world.apm_home, env=world.env)
    assert audit.returncode == 1, _evidence(audit)

    approved = world.run_hint(installed.stdout, r"Re-approve: (apm approve -g \S+)")
    assert approved.returncode == 0, _evidence(approved)
    deployed = world.apm("install", "-g")
    assert deployed.returncode == 0, _evidence(deployed)
    assert _codex_registered(world, plugin), _evidence(deployed)
    for argv in (("install", "-g", "--frozen"),):
        replay = world.apm(*argv)
        assert replay.returncode == 0, _evidence(replay)
    audit = world.runner.run(("audit", "--ci"), cwd=world.apm_home, env=world.env)
    assert audit.returncode == 0, _evidence(audit)

    denied = world.apm("deny", "-g", str(plugin))
    assert denied.returncode == 0, _evidence(denied)
    retired = world.apm("install", "-g")
    assert retired.returncode == 0, _evidence(retired)
    assert not _codex_registered(world, plugin), _evidence(retired)


def test_parked_codex_bundle_record_survives_and_uninstall_clears_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, plugin = _codex_world(tmp_path, apm_binary_path)
    assert world.apm("install", "-g", str(plugin)).returncode == 0
    # A new process still sees the parked record.
    pending = world.apm("approve", "-g", "--pending")
    assert str(plugin) in pending.stdout, _evidence(pending)
    assert _row(world, plugin)["exec_status"] == "gated_pending_approval"

    removed = world.apm("uninstall", "-g", str(plugin))
    assert removed.returncode == 0, _evidence(removed)
    lock_path = world.manifest.parent / "apm.lock.yaml"
    rows = _lock(world).get("dependencies") or [] if lock_path.is_file() else []
    assert not any(r.get("local_path") == str(plugin) for r in rows)
    assert not (world.apm_home / "apm_modules" / "_local" / "p-native-codex").exists()
    gone = world.apm("approve", "-g", str(plugin))
    assert gone.returncode == 1, _evidence(gone)


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
