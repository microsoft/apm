"""Installed-CLI contracts for the lockfile commit point.

A command that is interrupted (SIGINT or SIGTERM) after it deployed files and
before it commits unwinds as a failure: the install transaction restores
``apm.yml``, its staging and the audit-gated deploy snapshot, and the staged
lockfile is discarded. A process
killed at the same point leaves deployed files the lockfile does not record:
``apm audit --ci`` reports that state, and the next install adopts the
matching bytes and converges. An install whose MCP write collides on one
target records exactly what reached disk and reports the written and the
colliding connections without claiming success.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.integration._fault_injection_kit import FaultWorld, build_world, evidence
from tests.utils.artifact_snapshot import assert_unchanged
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


def _sigterm_after_an_unsnapshotted_deploy(
    tmp_path: Path, apm_binary_path: Path
) -> tuple[FaultWorld, str, str]:
    """SIGTERM a plain install (no deploy snapshot) at its commit point."""
    world = build_world(tmp_path, apm_binary_path)
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    deployed = ".github/agents/alpha.agent.md"
    process = _held_at_commit(world, apm_binary_path, tmp_path / "held", "install", "--no-policy")
    assert (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")
    os.killpg(process.pid, signal.SIGTERM)
    output, _ = process.communicate(timeout=120)
    assert process.returncode != 0, output
    return world, deployed, output


@pytest.mark.xfail(
    strict=True,
    reason="#4282: SIGTERM after a deploy without a snapshot leaves files the lock does not record",
)
def test_sigterm_without_a_deploy_snapshot_keeps_the_lock_equal_to_disk(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _deployed, output = _sigterm_after_an_unsnapshotted_deploy(tmp_path, apm_binary_path)
    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, f"{output}\n{evidence(audit)}"


def test_sigterm_without_a_deploy_snapshot_is_reported_by_audit_and_converged(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, deployed, _output = _sigterm_after_an_unsnapshotted_deploy(tmp_path, apm_binary_path)

    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode != 0, evidence(audit)
    assert f"hash-drift: {deployed}" in audit.stdout + audit.stderr, evidence(audit)

    world.assert_converges()
    converged = [world.snapshot(root) for root in _TARGET_ROOTS]
    lock = world.lockfile_bytes()
    repeat = world.install()
    assert repeat.returncode == 0, evidence(repeat)
    assert world.lockfile_bytes() == lock
    for snapshot, root in zip(converged, _TARGET_ROOTS, strict=True):
        assert_unchanged(snapshot, world.snapshot(root))


def test_a_failed_lockfile_write_is_reported_by_audit_and_converged(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4164: a lockfile write that fails after deploy leaves state audit reports."""
    world = build_world(tmp_path, apm_binary_path)
    lock = world.lockfile_bytes()
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    deployed = ".github/agents/alpha.agent.md"

    failed = world.runner.run(
        ("install", "--no-policy"),
        cwd=world.project,
        env=dict(world.env, APM_TEST_FAIL_LOCK_REPLACE="1"),
    )
    assert failed.returncode != 0, evidence(failed)
    assert "apm.lock.yaml" in failed.stdout + failed.stderr, evidence(failed)
    assert world.lockfile_bytes() == lock
    assert (world.project / deployed).read_text(encoding="ascii").endswith("v2\n")

    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode != 0, evidence(audit)
    assert f"hash-drift: {deployed}" in audit.stdout + audit.stderr, evidence(audit)

    world.assert_converges()
    assert world.lockfile_bytes() != lock


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
    else:
        assert "hash-drift: .github/agents/alpha.agent.md" in reported, evidence(audit)

    world.assert_converges()
    assert world.lockfile_bytes() != lock


@pytest.mark.xfail(
    strict=True,
    reason="#4282: a killed audit-gated install leaves its deploy snapshot under APM_HOME",
)
def test_the_next_install_reaps_a_killed_installs_deploy_snapshot(
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
    assert any(snapshots.glob("deploy-*")), "the killed install left no snapshot to reap"

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


def test_a_partial_mcp_collision_reports_each_write_and_records_what_reached_disk(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, foreign = _mcp_collision_world(tmp_path, apm_binary_path)
    original = foreign.read_bytes()

    result = world.install("--target", "claude,codex")
    output = " ".join((result.stdout + result.stderr).split())

    assert result.returncode != 0, evidence(result)
    assert foreign.read_bytes() == original
    assert "conflicts with the existing claude connection" in output, evidence(result)
    assert "beta-srv -> Codex CLI (configured)" in output, evidence(result)
    assert "MCP installation incomplete: beta-srv (claude)" in output, evidence(result)
    assert not re.search(r"\bInstalled \d+", output), evidence(result)
    lock = LockFile.read(world.project / "apm.lock.yaml")
    assert lock is not None, evidence(result)
    assert lock.mcp_target_servers == {"codex": ["beta-srv"]}, evidence(result)
    recorded = {
        path for dep in lock.get_package_dependencies() for path in dep.deployed_file_hashes
    }
    assert ".claude/agents/alpha.md" in recorded, evidence(result)
    assert all((world.project / path).exists() for path in recorded), evidence(result)


@pytest.mark.xfail(strict=True, reason="#4268: MCP collisions are detected at the MCP write")
def test_a_colliding_mcp_install_is_refused_before_any_file_is_written(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _foreign = _mcp_collision_world(tmp_path, apm_binary_path)

    result = world.install("--target", "claude,codex")

    assert result.returncode != 0, evidence(result)
    assert world.lockfile_bytes() is None, evidence(result)
    assert not (world.project / ".claude/agents").exists(), evidence(result)
    assert not (world.project / ".codex/config.toml").exists(), evidence(result)


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


def test_a_failed_mcp_reconcile_after_native_registration_keeps_the_lock_equal_to_disk(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4329: native registration commits the lock, then the MCP reconcile fails."""
    world = _native_plugin_with_colliding_mcp(tmp_path, apm_binary_path)

    failed = _user(world, "install", "-g", "--no-policy")

    assert failed.returncode != 0, evidence(failed)
    output = " ".join((failed.stdout + failed.stderr).split())
    assert "conflicts with the existing claude connection" in output, evidence(failed)
    assert f"MCP installation incomplete: {_COLLIDING_SERVER} (claude)" in output, evidence(failed)
    lock = _assert_user_lock_equals_disk(world)
    assert any(dep.native_plugin for dep in lock.get_package_dependencies()), evidence(failed)
    audit = _user_audit(world)
    frozen = _user(world, "install", "-g", "--frozen", "--no-policy")
    assert (audit.returncode == 0) == (frozen.returncode == 0), (
        f"{evidence(audit)}\n{evidence(frozen)}"
    )
    _assert_user_lock_equals_disk(world)

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
