"""Installed-CLI contracts for the lockfile commit point.

A command that fails or is interrupted (SIGINT or SIGTERM) after it deployed
files and before its lockfile commits unwinds as a failure: the install
transaction restores ``apm.yml``, its staging and every path the integrators
wrote (the lazy deploy journal, #4282), and the staged lockfile is discarded,
so disk and ``apm.lock.yaml`` keep their pre-command bytes. A process killed
at the same point leaves deployed files the lockfile does not record:
``apm audit --ci`` reports that state, and the next install adopts the
matching bytes, converges and reaps the killed install's journal. An install whose MCP write collides on one
target records exactly what reached disk and reports the written and the
colliding connections without claiming success.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.integration._fault_injection_kit import FaultWorld, build_world, evidence
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.lifecycle_evidence import expect_output_violation
from tests.utils.lockfile_write_counter import INOTIFY_AVAILABLE, count_lockfile_writes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise(
        "P-INV-I1",
        "P-OWN-process-interrupt-signals",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="POSIX signals"),
]

_DELTA_SKILL = ".claude/skills/delta-skill-0/SKILL.md"
_NEW_TARGET = "opencode"
_NEW_ROOT = ".opencode"
_TARGET_ROOTS = (".github", ".claude", ".cursor", ".agents")


def _held_at_commit(
    world: FaultWorld, binary: Path, marker: Path, *args: str
) -> subprocess.Popen[str]:
    env = dict(world.env, APM_TEST_LOCK_COMMIT=f"hold:{marker}")
    process = subprocess.Popen(
        (str(binary), *args),
        cwd=world.project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 240
    while process.poll() is None and not marker.exists():
        assert time.monotonic() < deadline, "install never reached its commit point"
        time.sleep(0.02)
    assert process.poll() is None, process.communicate()[0]
    return process


@pytest.mark.parametrize("interrupt", ["SIGINT", "SIGTERM"])
def test_interrupt_at_the_commit_point_restores_deploys_and_discards_the_lock(
    tmp_path: Path, apm_binary_path: Path, interrupt: str
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    enabled = world.apm("experimental", "enable", "external_scanners")
    assert enabled.returncode == 0, evidence(enabled)
    delta = world.write_package("delta")
    manifest = (world.project / "apm.yml").read_bytes()
    lock = world.lockfile_bytes()
    before = [world.snapshot(root) for root in _TARGET_ROOTS]

    process = _held_at_commit(
        world,
        apm_binary_path,
        tmp_path / "held",
        *("install", "--no-policy", "--audit", "block", delta),
    )
    assert (world.project / _DELTA_SKILL).is_file(), "the install deployed before its commit"
    os.killpg(process.pid, getattr(signal, interrupt))
    output, _ = process.communicate(timeout=120)

    assert process.returncode != 0, output
    assert "interrupted" in output.lower(), output
    assert "Deployed files restored to their previous state." in output, output
    assert (world.project / "apm.yml").read_bytes() == manifest, output
    assert world.lockfile_bytes() == lock, output
    for snapshot, root in zip(before, _TARGET_ROOTS, strict=True):
        assert_unchanged(snapshot, world.snapshot(root))
    assert not (world.project / "apm_modules/.apm-resolution-staging").exists()
    world.assert_user_file_intact()

    retry = world.install(delta)
    assert retry.returncode == 0, evidence(retry)
    assert "delta" in (world.lockfile_bytes() or b"").decode("utf-8")
    world.assert_converges()


def _target_state(world: FaultWorld) -> list[ArtifactSnapshot]:
    return [world.snapshot(root) for root in _TARGET_ROOTS]


def _assert_pre_command_state(
    world: FaultWorld, before: list[ArtifactSnapshot], lock: bytes | None, output: str
) -> None:
    """Deployed files and ``apm.lock.yaml`` hold their pre-command bytes."""
    assert "Deployed files restored to their previous state." in output, output
    assert world.lockfile_bytes() == lock, output
    for snapshot, after in zip(before, _target_state(world), strict=True):
        assert_unchanged(snapshot, after)
    world.assert_user_file_intact()


def _sigterm_after_an_unaudited_deploy(
    tmp_path: Path, apm_binary_path: Path
) -> tuple[FaultWorld, str, str, list[ArtifactSnapshot], bytes | None]:
    """SIGTERM a plain install (install-time audit off) at its commit point."""
    world = build_world(tmp_path, apm_binary_path)
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    deployed = ".github/agents/alpha.agent.md"
    before, lock = _target_state(world), world.lockfile_bytes()
    process = _held_at_commit(world, apm_binary_path, tmp_path / "held", "install", "--no-policy")
    assert (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")
    os.killpg(process.pid, signal.SIGTERM)
    output, _ = process.communicate(timeout=120)
    assert process.returncode != 0, output
    return world, deployed, output, before, lock


def test_sigterm_without_a_deploy_snapshot_keeps_the_lock_equal_to_disk(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _deployed, output, _before, _lock = _sigterm_after_an_unaudited_deploy(
        tmp_path, apm_binary_path
    )
    # Undo the source edit made before the command, so audit judges lock vs disk only.
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii").removesuffix("v2\n"), encoding="ascii")
    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, f"{output}\n{evidence(audit)}"


def test_sigterm_without_an_audit_restores_deploys_and_the_lock(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4282: every install journals its writes, not only an audit-gated one."""
    world, deployed, output, before, lock = _sigterm_after_an_unaudited_deploy(
        tmp_path, apm_binary_path
    )

    _assert_pre_command_state(world, before, lock, output)
    assert not (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")

    world.assert_converges()
    assert (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")
    converged = [world.snapshot(root) for root in _TARGET_ROOTS]
    lock = world.lockfile_bytes()
    repeat = world.install()
    assert repeat.returncode == 0, evidence(repeat)
    assert world.lockfile_bytes() == lock
    for snapshot, root in zip(converged, _TARGET_ROOTS, strict=True):
        assert_unchanged(snapshot, world.snapshot(root))


def test_a_failed_lockfile_write_restores_deploys_and_the_lock(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4164, #4282: a lockfile write that fails after deploy restores the deploys."""
    world = build_world(tmp_path, apm_binary_path)
    lock = world.lockfile_bytes()
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    deployed = ".github/agents/alpha.agent.md"
    before = _target_state(world)

    failed = world.runner.run(
        ("install", "--no-policy"),
        cwd=world.project,
        env=dict(world.env, APM_TEST_FAIL_LOCK_REPLACE="1"),
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, evidence(failed)
    assert "apm.lock.yaml" in output, evidence(failed)
    _assert_pre_command_state(world, before, lock, output)
    assert not (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")

    audit = world.apm("audit", "--ci", "--no-policy")
    assert f"hash-drift: {deployed}" not in audit.stdout + audit.stderr, evidence(audit)

    world.assert_converges()
    assert world.lockfile_bytes() != lock


def test_a_failure_after_deploy_leaves_every_byte_as_it_was(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4282: a new package deploys, then the lockfile write fails; nothing changed."""
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    manifest = (world.project / "apm.yml").read_bytes()
    lock = world.lockfile_bytes()
    before = _target_state(world)
    receipts = ArtifactSnapshot.capture(world.isolated.config_root / "deployment-receipts")

    failed = world.runner.run(
        ("install", "--no-policy", delta),
        cwd=world.project,
        env=dict(world.env, APM_TEST_FAIL_LOCK_REPLACE="1"),
    )

    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, evidence(failed)
    _assert_pre_command_state(world, before, lock, output)
    assert (world.project / "apm.yml").read_bytes() == manifest, output
    assert not (world.project / _DELTA_SKILL).exists(), output
    assert_unchanged(
        receipts, ArtifactSnapshot.capture(world.isolated.config_root / "deployment-receipts")
    )
    snapshots = world.isolated.config_root / "install-snapshots"
    assert not any(snapshots.glob("deploy-*")), output

    retry = world.install(delta)
    assert retry.returncode == 0, evidence(retry)
    assert (world.project / _DELTA_SKILL).is_file(), evidence(retry)
    world.assert_converges()


@pytest.mark.parametrize(
    "command",
    [("update", "--yes"), ("deps", "update")],
    ids=["update", "deps-update"],
)
def test_a_failed_lockfile_write_after_an_update_restores_deploys_and_the_lock(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    """#4664: update entry points keep the deploy journal until their lockfile commits."""
    world, lock, before = _declare_delta_and_a_new_target(tmp_path, apm_binary_path)

    failed = world.runner.run(
        command,
        cwd=world.project,
        env=dict(world.env, APM_TEST_FAIL_LOCK_REPLACE="1"),
    )

    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, evidence(failed)
    assert "apm.lock.yaml" in output, evidence(failed)
    _assert_update_left_nothing(world, before, lock, output)


def _declare_delta_and_a_new_target(
    tmp_path: Path, apm_binary_path: Path
) -> tuple[FaultWorld, bytes | None, list[ArtifactSnapshot]]:
    """Declare a new package and a new target whose root the update must create."""
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    manifest = world.project / "apm.yml"
    text = manifest.read_text(encoding="ascii").replace("cursor]", f"cursor, {_NEW_TARGET}]")
    manifest.write_text(text + f"    - {delta}\n", encoding="ascii")
    assert not (world.project / _NEW_ROOT).exists()
    return world, world.lockfile_bytes(), _target_state(world)


def _assert_update_left_nothing(
    world: FaultWorld, before: list[ArtifactSnapshot], lock: bytes | None, output: str
) -> None:
    _assert_pre_command_state(world, before, lock, output)
    assert not (world.project / _DELTA_SKILL).exists(), output
    assert not (world.project / _NEW_ROOT).exists(), output
    world.assert_converges()
    assert (world.project / _DELTA_SKILL).is_file()
    assert (world.project / _NEW_ROOT).is_dir()


@pytest.mark.parametrize("interrupt", ["SIGINT", "SIGTERM"])
def test_an_update_interrupted_at_the_commit_point_restores_deploys_and_the_lock(
    tmp_path: Path, apm_binary_path: Path, interrupt: str
) -> None:
    """#4664: the restore also runs when the update is interrupted before its lockfile commits."""
    world, lock, before = _declare_delta_and_a_new_target(tmp_path, apm_binary_path)
    process = _held_at_commit(world, apm_binary_path, tmp_path / "held", "update", "--yes")
    os.killpg(process.pid, getattr(signal, interrupt))
    output, _ = process.communicate(timeout=120)
    assert process.returncode != 0, output
    _assert_update_left_nothing(world, before, lock, output)


@pytest.mark.parametrize("audited", [False, True], ids=["plain", "audited"])
@pytest.mark.parametrize("change", ["new-package", "changed-package"])
def test_a_killed_install_is_reported_by_audit_and_converged_by_the_next_install(
    tmp_path: Path, apm_binary_path: Path, change: str, audited: bool
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    audit_args: tuple[str, ...] = ()
    if audited:
        enabled = world.apm("experimental", "enable", "external_scanners")
        assert enabled.returncode == 0, evidence(enabled)
        audit_args = ("--audit", "block")
    lock = world.lockfile_bytes()
    if change == "new-package":
        args: tuple[str, ...] = (world.write_package("delta"),)
        deployed = world.project / _DELTA_SKILL
    else:
        args = ()
        agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
        agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
        deployed = world.project / ".github/agents/alpha.agent.md"

    with expect_output_violation("I8", reason="the process is killed at its commit point"):
        killed = world.runner.run(
            ("install", "--no-policy", *audit_args, *args),
            cwd=world.project,
            env=dict(world.env, APM_TEST_LOCK_COMMIT="kill"),
        )
    assert killed.returncode == 137, evidence(killed)
    assert world.lockfile_bytes() == lock
    assert deployed.is_file(), evidence(killed)
    if change == "changed-package":
        assert deployed.read_text(encoding="ascii").endswith("v2\n")

    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode != 0, evidence(audit)
    reported = audit.stdout + audit.stderr
    if change == "new-package":
        assert "./packages/delta: not found in lockfile" in reported, evidence(audit)
        # #5454: the next command restores the pre-install apm.yml and names the
        # command that adds the package again, instead of locking the new package.
        restored = world.install()
        assert restored.returncode == 0, evidence(restored)
        output = " ".join((restored.stdout + restored.stderr).split())
        assert f"Run 'apm install {args[0]}' to add it again" in output, evidence(restored)
        assert world.lockfile_bytes() == lock
        assert not deployed.exists(), evidence(restored)
        world.assert_converges()
        return
    assert "hash-drift: .github/agents/alpha.agent.md" in reported, evidence(audit)

    world.assert_converges()
    assert world.lockfile_bytes() != lock


def test_the_next_install_replays_a_killed_installs_deploy_snapshot(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    enabled = world.apm("experimental", "enable", "external_scanners")
    assert enabled.returncode == 0, evidence(enabled)
    delta = world.write_package("delta")
    with expect_output_violation("I8", reason="the process is killed at its commit point"):
        killed = world.runner.run(
            ("install", "--no-policy", "--audit", "block", delta),
            cwd=world.project,
            env=dict(world.env, APM_TEST_LOCK_COMMIT="kill"),
        )
    assert killed.returncode == 137, evidence(killed)
    snapshots = world.isolated.config_root / "install-snapshots"
    assert any(snapshots.glob("deploy-*")), "the killed install left no snapshot to replay"

    world.assert_converges()
    assert not any(snapshots.glob("deploy-*")), sorted(snapshots.iterdir())


def _mcp_collision_world(tmp_path: Path, apm_binary_path: Path) -> tuple[FaultWorld, Path]:
    world = build_world(
        tmp_path,
        apm_binary_path,
        mcp_package="beta",
        targets=("claude", "codex"),
        installed=False,
    )
    world.env["CODEX_HOME"] = str(world.isolated.home / ".codex")
    foreign = world.project / ".mcp.json"
    foreign.write_text(
        json.dumps({"mcpServers": {"beta-srv": {"command": "personal"}}}), encoding="utf-8"
    )
    return world, foreign


def test_a_colliding_mcp_install_is_refused_before_any_file_is_written(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The install preflight finds the collision, so nothing reaches disk (#4268)."""
    world, foreign = _mcp_collision_world(tmp_path, apm_binary_path)
    original = foreign.read_bytes()

    result = world.install("--target", "claude,codex")
    output = " ".join((result.stdout + result.stderr).split())

    assert result.returncode != 0, evidence(result)
    assert "conflicts with the existing claude connection" in output, evidence(result)
    assert "MCP installation incomplete: beta-srv (claude)" in output, evidence(result)
    assert "Codex CLI (configured)" not in output, evidence(result)
    assert foreign.read_bytes() == original
    assert world.lockfile_bytes() is None, evidence(result)
    assert not (world.project / ".claude/agents").exists(), evidence(result)
    assert not (world.project / ".codex/config.toml").exists(), evidence(result)
    assert not (Path(world.env["CODEX_HOME"]) / "config.toml").exists(), evidence(result)


@pytest.mark.parametrize("change", ["declaration", "user-edit"])
def test_a_recorded_mcp_server_collision_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, change: str
) -> None:
    """A server the lockfile records is projected too (#4741).

    A changed declaration still updates APM's own entry (no false refusal);
    a user edit to the recorded entry refuses before any file is written.
    """
    world = build_world(
        tmp_path, apm_binary_path, mcp_package="beta", targets=("claude", "codex"), installed=False
    )
    world.env["CODEX_HOME"] = str(world.isolated.home / ".codex")
    first = world.install("--target", "claude,codex")
    assert first.returncode == 0, evidence(first)
    mcp = world.project / ".mcp.json"
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    if change == "declaration":
        manifest = world.project / "packages/beta/apm.yml"
        text = manifest.read_text(encoding="ascii")
        manifest.write_text(text.replace("beta-mcp", "beta-mcp-v2"), encoding="ascii")
    else:
        data = json.loads(mcp.read_text(encoding="utf-8"))
        data["mcpServers"]["beta-srv"] = {"command": "personal"}
        mcp.write_text(json.dumps(data), encoding="utf-8")
    mcp_before = mcp.read_bytes()
    lock_before = world.lockfile_bytes()

    result = world.install("--target", "claude,codex")
    output = " ".join((result.stdout + result.stderr).split())

    deployed = (world.project / ".claude/agents/alpha.md").read_text(encoding="utf-8")
    if change == "declaration":
        assert result.returncode == 0, evidence(result)
        assert deployed.endswith("v2\n"), evidence(result)
        servers = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
        assert servers["beta-srv"]["command"] == "beta-mcp-v2", evidence(result)
    else:
        assert result.returncode != 0, evidence(result)
        assert "conflicts with the existing claude connection" in output, evidence(result)
        assert not deployed.endswith("v2\n"), evidence(result)
        assert mcp.read_bytes() == mcp_before, evidence(result)
        assert world.lockfile_bytes() == lock_before, evidence(result)


def test_dry_run_waits_for_a_running_install(tmp_path: Path, apm_binary_path: Path) -> None:
    """A dry-run resolves into apm_modules staging, so it serializes with a running install."""
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    held = _held_at_commit(
        world, apm_binary_path, tmp_path / "held", "install", "--no-policy", delta
    )
    preview = subprocess.Popen(
        (str(apm_binary_path), "install", "--dry-run", "--no-policy"),
        cwd=world.project,
        env=world.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(5)
        assert preview.poll() is None, preview.communicate()[0]
    finally:
        os.killpg(held.pid, signal.SIGTERM)
        held.communicate(timeout=120)
    output, _ = preview.communicate(timeout=240)
    assert preview.returncode == 0, output
    assert "Another APM operation is running" in output, output


@pytest.mark.skipif(not INOTIFY_AVAILABLE, reason="counting lockfile writes requires Linux inotify")
def test_post_install_script_sees_the_final_lockfile_written_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """post-install fires after MCP/LSP reconciliation, so it sees the one committed lockfile."""
    world = build_world(
        tmp_path, apm_binary_path, mcp_package="beta", targets=("copilot",), installed=False
    )
    manifest = world.project / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="ascii")
        + "lifecycle:\n  post-install:\n    - type: command\n"
        + '      bash: "cp apm.lock.yaml seen.lock.yaml"\n      timeoutSec: 60\n',
        encoding="ascii",
    )
    trust = world.apm("lifecycle", "trust")
    assert trust.returncode == 0, evidence(trust)

    with count_lockfile_writes(world.project) as counter:
        installed = world.install("--trust-transitive-mcp")

    assert installed.returncode == 0, evidence(installed)
    assert counter.writes == 1, (counter.events, evidence(installed))
    seen = world.project / "seen.lock.yaml"
    assert seen.read_bytes() == world.lockfile_bytes(), evidence(installed)
    assert "beta-srv" in seen.read_text(encoding="utf-8")


_NATIVE_PLUGIN = "commit-point-native"
_COLLIDING_SERVER = "commit-point-srv"


def _native_plugin_with_colliding_mcp(tmp_path: Path, apm_binary_path: Path) -> FaultWorld:
    """A user-scope native Claude plugin plus an MCP server whose name collides."""
    world = build_world(tmp_path, apm_binary_path, packages=(), installed=False)
    home = world.isolated.home
    for variable in ("CLAUDE_CONFIG_DIR", "COPILOT_HOME", "CODEX_HOME"):
        world.env.pop(variable, None)
    plugin = world.isolated.package_root / _NATIVE_PLUGIN
    manifest = plugin / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": _NATIVE_PLUGIN}), encoding="utf-8")
    skill = plugin / "skills/commit-point-skill/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: commit-point-skill\ndescription: native\n---\nbody\n", encoding="ascii"
    )
    (world.isolated.config_root / "apm.yml").write_text(
        json.dumps(
            {
                "name": "user",
                "version": "1.0.0",
                "targets": ["claude"],
                "dependencies": {
                    "apm": [str(plugin)],
                    "mcp": [
                        {
                            "name": _COLLIDING_SERVER,
                            "registry": False,
                            "transport": "stdio",
                            "command": "commit-point-mcp",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {_COLLIDING_SERVER: {"command": "personal"}}}),
        encoding="utf-8",
    )
    return world


def _user(world: FaultWorld, *args: str):
    return world.runner.run(args, cwd=world.isolated.work_root, env=world.env)


def _user_audit(world: FaultWorld):
    return world.runner.run(
        ("audit", "--ci", "--no-policy"), cwd=world.isolated.config_root, env=world.env
    )


def _assert_user_lock_equals_disk(world: FaultWorld) -> LockFile:
    """Every recorded native root, file and MCP server is on disk; nothing else is."""
    home = world.isolated.home
    lock = LockFile.read(world.isolated.config_root / "apm.lock.yaml")
    assert lock is not None
    deps = lock.get_package_dependencies()
    records = {dep.native_plugin.name: dep.native_plugin for dep in deps if dep.native_plugin}
    plugins = home / ".agents/plugins"
    # .claude-plugin/ holds APM's marketplace manifest, not a plugin.
    on_disk = (
        {path.name for path in plugins.iterdir() if path.name != ".claude-plugin"}
        if plugins.exists()
        else set()
    )
    assert on_disk == set(records), (on_disk, records)
    settings_path = home / ".claude/settings.json"
    settings = json.loads(settings_path.read_text("utf-8")) if settings_path.exists() else {}
    enabled = {
        key
        for key in settings.get("enabledPlugins", {})
        if any(key == f"{name}@{rec.marketplace}" for name, rec in records.items())
    }
    assert enabled == {f"{name}@{rec.marketplace}" for name, rec in records.items()}, settings
    for record in records.values():
        assert (home / record.root / ".claude-plugin/plugin.json").is_file(), record
    recorded = {path for dep in deps for path in dep.deployed_files}
    assert all((home / path).exists() for path in recorded), recorded
    loose = home / ".claude/skills"
    unrecorded = sorted(
        path.relative_to(home).as_posix()
        for path in (loose.rglob("*") if loose.exists() else ())
        if path.is_file() and path.relative_to(home).as_posix() not in recorded
    )
    assert not unrecorded, unrecorded
    claude_json = json.loads((home / ".claude.json").read_text("utf-8"))
    configured = claude_json.get("mcpServers", {})
    for server in lock.mcp_target_servers.get("claude", []):
        assert server in configured, (server, configured)
    if configured.get(_COLLIDING_SERVER, {}).get("command") == "personal":
        assert _COLLIDING_SERVER not in lock.mcp_target_servers.get("claude", []), lock
    return lock


def test_a_recorded_native_plugin_mcp_collision_is_refused_and_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4741 (from #4268): a recorded claim the user edits refuses before any write.

    The lock and the user's ~/.claude.json keep their bytes; clearing the
    user's entry then converges with the lock equal to disk.
    """
    world = _native_plugin_with_colliding_mcp(tmp_path, apm_binary_path)
    claude_json = world.isolated.home / ".claude.json"
    claude_json.write_text("{}", encoding="utf-8")
    clean = _user(world, "install", "-g", "--no-policy")
    assert clean.returncode == 0, evidence(clean)
    _assert_user_lock_equals_disk(world)
    user_entry = {"mcpServers": {_COLLIDING_SERVER: {"command": "personal"}}}
    claude_json.write_text(json.dumps(user_entry), encoding="utf-8")
    edited = claude_json.read_bytes()
    lock_path = world.isolated.config_root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    state_before = sorted(
        path.relative_to(world.isolated.home).as_posix()
        for path in world.isolated.home.rglob("*")
        if ".apm/cache" not in path.as_posix()
    )

    failed = _user(world, "install", "-g", "--no-policy")

    assert failed.returncode != 0, evidence(failed)
    output = " ".join((failed.stdout + failed.stderr).split())
    assert "conflicts with the existing claude connection" in output, evidence(failed)
    assert "Configuring for" not in output, evidence(failed)
    assert lock_path.read_bytes() == lock_before, evidence(failed)
    assert claude_json.read_bytes() == edited, evidence(failed)
    assert (
        sorted(
            path.relative_to(world.isolated.home).as_posix()
            for path in world.isolated.home.rglob("*")
            if ".apm/cache" not in path.as_posix()
        )
        == state_before
    ), evidence(failed)

    claude_json.write_text("{}", encoding="utf-8")
    converged = _user(world, "install", "-g", "--no-policy")
    assert converged.returncode == 0, evidence(converged)
    lock = _assert_user_lock_equals_disk(world)
    assert _COLLIDING_SERVER in lock.mcp_target_servers.get("claude", []), evidence(converged)


def test_a_failed_mcp_reconcile_after_native_registration_keeps_the_lock_equal_to_disk(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4329: a native plugin install whose MCP server collides keeps the lock equal to disk.

    The server is self-defined and unrecorded, so the install preflight refuses
    it before native registration or any other write (#4268); clearing the
    user's entry then converges.
    """
    world = _native_plugin_with_colliding_mcp(tmp_path, apm_binary_path)
    claude_json = world.isolated.home / ".claude.json"
    original = claude_json.read_bytes()

    failed = _user(world, "install", "-g", "--no-policy")

    assert failed.returncode != 0, evidence(failed)
    output = " ".join((failed.stdout + failed.stderr).split())
    assert "conflicts with the existing claude connection" in output, evidence(failed)
    assert f"MCP installation incomplete: {_COLLIDING_SERVER} (claude)" in output, evidence(failed)
    assert not (world.isolated.config_root / "apm.lock.yaml").exists(), evidence(failed)
    assert claude_json.read_bytes() == original, evidence(failed)

    (world.isolated.home / ".claude.json").write_text("{}", encoding="utf-8")
    converged = _user(world, "install", "-g", "--no-policy")
    assert converged.returncode == 0, evidence(converged)
    lock = _assert_user_lock_equals_disk(world)
    assert _COLLIDING_SERVER in lock.mcp_target_servers.get("claude", []), evidence(converged)
    settled = (world.isolated.config_root / "apm.lock.yaml").read_bytes()
    repeat = _user(world, "install", "-g", "--no-policy")
    assert repeat.returncode == 0, evidence(repeat)
    assert (world.isolated.config_root / "apm.lock.yaml").read_bytes() == settled
    audit = _user_audit(world)
    assert audit.returncode == 0, evidence(audit)
    frozen = _user(world, "install", "-g", "--frozen", "--no-policy")
    assert frozen.returncode == 0, evidence(frozen)
