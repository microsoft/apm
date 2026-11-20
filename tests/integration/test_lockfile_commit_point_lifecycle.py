"""Installed-CLI contracts for the lockfile commit point.

A command that is interrupted (SIGINT or SIGTERM) after it deployed files and
before it commits unwinds as a failure: the install transaction restores
``apm.yml`` and its staging, and the staged lockfile is discarded. A process
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="POSIX signals"),
]

_DELTA_SKILL = ".claude/skills/delta-skill-0/SKILL.md"


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
def test_interrupt_at_the_commit_point_restores_the_manifest_and_discards_the_lock(
    tmp_path: Path, apm_binary_path: Path, interrupt: str
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    manifest = (world.project / "apm.yml").read_bytes()
    lock = world.lockfile_bytes()

    process = _held_at_commit(
        world, apm_binary_path, tmp_path / "held", "install", "--no-policy", delta
    )
    assert (world.project / _DELTA_SKILL).is_file(), "the install deployed before its commit"
    os.killpg(process.pid, getattr(signal, interrupt))
    output, _ = process.communicate(timeout=120)

    assert process.returncode != 0, output
    assert "interrupted" in output.lower(), output
    assert (world.project / "apm.yml").read_bytes() == manifest, output
    assert world.lockfile_bytes() == lock, output
    assert not (world.project / "apm_modules/.apm-resolution-staging").exists()
    world.assert_user_file_intact()

    retry = world.install(delta)
    assert retry.returncode == 0, evidence(retry)
    assert "delta" in (world.lockfile_bytes() or b"").decode("utf-8")
    world.assert_converges()


@pytest.mark.parametrize("change", ["new-package", "changed-package"])
def test_a_killed_install_is_reported_by_audit_and_converged_by_the_next_install(
    tmp_path: Path, apm_binary_path: Path, change: str
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    lock = world.lockfile_bytes()
    if change == "new-package":
        args: tuple[str, ...] = (world.write_package("delta"),)
        deployed = world.project / _DELTA_SKILL
    else:
        args = ()
        agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
        agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
        deployed = world.project / ".github/agents/alpha.agent.md"

    killed = world.runner.run(
        ("install", "--no-policy", *args),
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
