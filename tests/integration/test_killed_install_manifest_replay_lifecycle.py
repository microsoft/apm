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

from apm_cli.utils.yaml_io import load_yaml
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


def _records(world: FaultWorld) -> list[Path]:
    return sorted((world.isolated.config_root / "install-snapshots").glob("manifest-*"))


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
    assert not _records(world), "the replayed manifest record was kept"
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
    assert _records(world), "the kill left no manifest record to replay"

    _assert_restored(world, manifest, lock, delta)
    added = world.install(delta)
    assert added.returncode == 0, evidence(added)
    assert not _records(world), "a committed install kept its manifest record"
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
    assert not _records(world)


def test_an_apm_yml_edited_after_the_kill_is_kept_and_named(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Replay undoes only the bytes the killed install wrote; a user's later edit stays."""
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    _kill_at_commit(world, delta)
    edited = (world.project / "apm.yml").read_bytes() + b"# hand edit\n"
    (world.project / "apm.yml").write_bytes(edited)

    result = world.install("--frozen")
    output = _text(result)
    assert f"An interrupted 'apm install {delta}' never locked" in output, evidence(result)
    assert "Run 'apm install' to lock what apm.yml declares" in output, evidence(result)
    assert (world.project / "apm.yml").read_bytes() == edited
    again = world.install("--frozen")
    assert "An interrupted" not in _text(again), evidence(again)


def test_a_killed_install_that_created_apm_yml_removes_it_on_the_next_command(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path, installed=False)
    (world.project / "apm.yml").unlink()
    delta = world.write_package("delta")
    _kill_at_commit(world, "--target", "claude", delta)
    assert (world.project / "apm.yml").is_file()

    with expect_output_violation("I8", reason="no apm.yml: the command exits before install"):
        result = world.install("--frozen")
    output = _text(result)
    assert result.returncode != 0, evidence(result)
    assert f"Run 'apm install --target claude {delta}' to add it again" in output, evidence(result)
    assert "No apm.yml found" in output, evidence(result)
    assert _OUT_OF_SYNC not in output, evidence(result)
    assert not (world.project / "apm.yml").exists()

    # The retry bootstraps apm.yml again and keeps the --target it pins.
    retried = world.install("--target", "claude", delta)
    assert retried.returncode == 0, evidence(retried)
    targets = load_yaml(world.project / "apm.yml").get("targets")
    assert targets == ["claude"], (world.project / "apm.yml").read_text(encoding="utf-8")


def test_concurrent_commands_after_the_kill_restore_apm_yml_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    manifest, lock = (world.project / "apm.yml").read_bytes(), world.lockfile_bytes()
    delta = world.write_package("delta")
    _kill_at_commit(world, delta)

    command = (str(apm_binary_path), "install", "--no-policy", "--frozen")
    procs = [
        subprocess.Popen(
            command,
            cwd=world.project,
            env=world.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [" ".join(p.communicate(timeout=300)[0].split()) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outputs
    assert sum("to add it again" in out for out in outputs) == 1, outputs
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert world.lockfile_bytes() == lock


def test_a_killed_uninstall_keeps_apm_yml_and_lock_paired(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The other direction: uninstall flushes its lockfile before its commit point."""
    world = build_world(tmp_path, apm_binary_path)
    with expect_output_violation("I8", reason="the process is killed at its commit point"):
        killed = world.runner.run(
            ("uninstall", "./packages/alpha"),
            cwd=world.project,
            env=dict(world.env, APM_TEST_LOCK_COMMIT="kill"),
        )
    assert killed.returncode == _KILLED_EXIT, evidence(killed)
    frozen = world.install("--frozen")
    assert frozen.returncode == 0, evidence(frozen)
    assert _OUT_OF_SYNC not in _text(frozen), evidence(frozen)


def test_a_kill_between_the_apm_yml_write_and_its_seal_restores_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The add names the bytes before it writes them, so no seal is needed (#5454)."""
    world = build_world(tmp_path, apm_binary_path)
    manifest, lock = (world.project / "apm.yml").read_bytes(), world.lockfile_bytes()
    delta = world.write_package("delta")

    _kill_on_engine(world, "manifest+kill", delta)
    assert delta in _declared(world), "the kill came before apm.yml was written"

    _assert_restored(world, manifest, lock, delta)


def test_a_killed_dev_add_names_the_dev_retry(tmp_path: Path, apm_binary_path: Path) -> None:
    world = build_world(tmp_path, apm_binary_path)
    manifest = (world.project / "apm.yml").read_bytes()
    delta = world.write_package("delta")
    _kill_at_commit(world, "--dev", delta)

    result = world.install("--frozen")
    assert result.returncode == 0, evidence(result)
    assert f"Run 'apm install --dev {delta}' to add it again" in _text(result), evidence(result)
    assert (world.project / "apm.yml").read_bytes() == manifest


def test_a_killed_root_install_restores_the_source_apm_yml(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``--root``: the source apm.yml and the deploy root's lockfile stay paired."""
    world = build_world(tmp_path, apm_binary_path, installed=False)
    deploy = world.project.parent / "deploy"
    deploy.mkdir()
    (deploy / ".claude").mkdir()
    first = world.install("--root", str(deploy))
    assert first.returncode == 0, evidence(first)
    manifest = (world.project / "apm.yml").read_bytes()
    lock = (deploy / "apm.lock.yaml").read_bytes()
    delta = str(world.project / world.write_package("delta"))

    _kill_at_commit(world, "--root", str(deploy), delta)
    assert delta in _declared(world), "the kill came before apm.yml was written"

    frozen = world.install("--root", str(deploy), "--frozen")
    assert frozen.returncode == 0, evidence(frozen)
    assert _OUT_OF_SYNC not in _text(frozen), evidence(frozen)
    assert f"Run 'apm install --root {deploy} {delta}' to add it again" in _text(frozen), evidence(
        frozen
    )
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert (deploy / "apm.lock.yaml").read_bytes() == lock


_BOOKKEEPING = frozenset({".apm-lifecycle.lock", "last_version_check", "device-id"})


# Install's declared HOME write set: the receipts that bind deployed files.
_DECLARED = ".apm/deployment-receipts"


def _home_state(home: Path) -> dict[str, bytes | None]:
    """Every path under HOME (``None`` for a directory) but bookkeeping and declared writes."""
    state = {}
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home).as_posix()
        if path.name in _BOOKKEEPING or rel == _DECLARED or rel.startswith(f"{_DECLARED}/"):
            continue
        state[rel] = path.read_bytes() if path.is_file() else None
    return state


def test_a_successful_add_leaves_home_byte_identical(tmp_path: Path, apm_binary_path: Path) -> None:
    """I5: the record lives only while the add runs; HOME keeps no trace of it."""
    world = build_world(tmp_path, apm_binary_path)
    delta = world.write_package("delta")
    before = _home_state(world.isolated.home)

    added = world.install(delta)

    assert added.returncode == 0, evidence(added)
    assert _home_state(world.isolated.home) == before
    assert not (world.isolated.config_root / "install-snapshots").exists()


def test_a_kill_during_the_apm_yml_publication_never_leaves_a_partial_file(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The add publishes apm.yml with one rename: a kill mid-write leaves the prior bytes."""
    world = build_world(tmp_path, apm_binary_path)
    manifest, lock = (world.project / "apm.yml").read_bytes(), world.lockfile_bytes()
    delta = world.write_package("delta")

    _kill_on_engine(
        world,
        "stage+kill",
        delta,
        APM_TEST_DEPLOY_KILL_PATH=f"{world.project.name}/apm.yml",
    )
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert _records(world), "the kill left no manifest record"

    frozen = world.install("--frozen")
    assert frozen.returncode == 0, evidence(frozen)
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert world.lockfile_bytes() == lock
    assert not _records(world)


def test_another_source_sharing_the_root_is_refused_until_the_first_replays(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Fail closed: B never overwrites A's record; the refusal names A and its command."""
    world = build_world(tmp_path, apm_binary_path, installed=False)
    deploy = world.project.parent / "deploy"
    (deploy / ".claude").mkdir(parents=True)
    assert world.install("--root", str(deploy)).returncode == 0
    manifest = (world.project / "apm.yml").read_bytes()
    delta = str(world.project / world.write_package("delta"))
    _kill_at_commit(world, "--root", str(deploy), delta)

    other = world.project.parent / "other"
    other.mkdir()
    (other / "apm.yml").write_text("name: other\nversion: 1.0.0\n", encoding="ascii")
    with expect_output_violation("I8", reason="the refusal exits before install"):
        refused = world.runner.run(
            ("install", "--no-policy", "--root", str(deploy), delta), cwd=other, env=world.env
        )
    output = _text(refused)
    assert refused.returncode != 0, evidence(refused)
    assert f"from {world.project} is still pending" in output, evidence(refused)
    assert f"Change to {world.project}, then run 'apm install --root {deploy}'" in output
    assert (other / "apm.yml").read_text(encoding="ascii") == "name: other\nversion: 1.0.0\n"
    assert _records(world), "the second source replaced the pending record"

    replayed = world.install("--root", str(deploy), "--frozen")
    assert replayed.returncode == 0, evidence(replayed)
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert not _records(world)
