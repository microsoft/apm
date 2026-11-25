"""APMLifecycle: a killed ``apm install <new-package>`` keeps apm.yml and the lock paired (#5454).

PROMISE: P-INV-I7 -- a command that does not commit leaves durable state as it
found it. ``apm install ./packages/delta`` writes the new entry to ``apm.yml``
before its lockfile commits. A process killed (SIGKILL, OOM, power loss) in
between leaves ``apm.yml`` declaring a package the lock has no row for, so
``apm install --frozen`` refused it as out of sync. The install transaction
records the pre-install ``apm.yml``; the next command restores it, under the
lifecycle lock and before it reads the manifest, exactly as it replays the
deploy journal, and names the command that adds the package again.

The kills are deterministic: ``APM_TEST_LOCK_COMMIT=kill`` exits at the lock
commit point (after deploy, before the lock write);
``tests/integration/_deploy_kill/sitecustomize.py`` exits mid-deploy or right
after the lock reached disk.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import FaultWorld, build_world, evidence
from tests.lifecycle import driver
from tests.utils.lifecycle_evidence import expect_output_violation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
    pytest.mark.skipif(os.name == "nt", reason="POSIX process kill"),
]

_KILL_HOOK = Path(__file__).with_name("_deploy_kill")
_KILLED_EXIT = 137
_DELTA_SKILL = ".claude/skills/delta-skill-0/SKILL.md"
_OUT_OF_SYNC = "out of sync"


def _text(result) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _kill_at_commit(world: FaultWorld, *args: str) -> None:
    with expect_output_violation("I8", reason="the process is killed at its commit point"):
        killed = world.runner.run(
            ("install", "--no-policy", *args),
            cwd=world.project,
            env=dict(world.env, APM_TEST_LOCK_COMMIT="kill"),
        )
    assert killed.returncode == _KILLED_EXIT, evidence(killed)


def _kill_on_engine(world: FaultWorld, fault: str, *args: str, **extra: str) -> None:
    """Run ``apm install`` on the engine with a ``_deploy_kill`` fault; it must be killed."""
    env = {**world.env, **extra, "APM_TEST_DEPLOY_FAULT": fault}
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_KILL_HOOK), env.get("PYTHONPATH", "")) if part
    )
    command = (*driver.driver_command("engine"), "install", "--no-policy", *args)
    result = subprocess.run(
        command, cwd=world.project, env=env, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == _KILLED_EXIT, (command, result.stdout, result.stderr)


def _declared(world: FaultWorld) -> str:
    return (world.project / "apm.yml").read_text(encoding="utf-8")


def _assert_restored(world: FaultWorld, manifest: bytes, lock: bytes | None, delta: str) -> None:
    """The next ``--frozen`` install restores apm.yml, keeps the lock and names the fix."""
    frozen = world.install("--frozen")
    output = _text(frozen)
    assert frozen.returncode == 0, evidence(frozen)
    assert _OUT_OF_SYNC not in output, evidence(frozen)
    assert f"apm install {delta}" in output, evidence(frozen)
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert world.lockfile_bytes() == lock
    assert not (world.project / _DELTA_SKILL).exists(), "the killed install's deploy was kept"
    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, evidence(audit)
    world.assert_user_file_intact()


def test_a_kill_at_the_lock_commit_point_restores_apm_yml_on_the_next_command(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    manifest, lock = (world.project / "apm.yml").read_bytes(), world.lockfile_bytes()
    delta = world.write_package("delta")

    _kill_at_commit(world, delta)
    assert delta in _declared(world), "the kill came before apm.yml was written"
    assert world.lockfile_bytes() == lock

    _assert_restored(world, manifest, lock, delta)
    added = world.install(delta)
    assert added.returncode == 0, evidence(added)
    world.assert_converges()
    assert (world.project / _DELTA_SKILL).is_file()


def test_a_kill_mid_deploy_restores_apm_yml_on_the_next_command(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    manifest, lock = (world.project / "apm.yml").read_bytes(), world.lockfile_bytes()
    delta = world.write_package("delta")

    _kill_on_engine(world, "deploy+kill", delta, APM_TEST_DEPLOY_KILL_AFTER="1")
    assert delta in _declared(world), "the kill came before apm.yml was written"

    _assert_restored(world, manifest, lock, delta)


def test_a_kill_after_the_lock_commit_keeps_the_new_package(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    lock = world.lockfile_bytes()
    delta = world.write_package("delta")

    _kill_on_engine(world, "settle+kill", delta)
    committed = world.lockfile_bytes()
    assert committed != lock, "the kill came before the lock reached disk"

    frozen = world.install("--frozen")
    assert frozen.returncode == 0, evidence(frozen)
    assert f"apm install {delta}" not in _text(frozen), evidence(frozen)
    assert delta in _declared(world)
    assert world.lockfile_bytes() == committed
    assert (world.project / _DELTA_SKILL).is_file()
