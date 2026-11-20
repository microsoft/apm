"""Installed-CLI contracts for the lockfile commit point.

A command that is interrupted (SIGINT or SIGTERM) after it deployed files and
before it commits unwinds as a failure: the install transaction restores
``apm.yml`` and its staging, and the staged lockfile is discarded. A process
killed at the same point leaves deployed files the lockfile does not record:
``apm audit --ci`` reports that state, and the next install adopts the
matching bytes and converges.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

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
