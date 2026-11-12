"""Installed-CLI contracts for faults injected mid-pipeline.

Each scenario installs a small local-package project, injects one fault
(read-only module store, a target root replaced by a file, a deployed path
replaced by a directory or a symlink, an unwritable nested compile output,
Ctrl+C during install) and asserts that the command fails with an actionable
exit code, ``apm.lock.yaml`` and user files stay unchanged, nothing is
written through a symlink, no staging leftovers remain, and a clean install
afterwards converges with ``apm audit --ci`` agreeing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import (
    build_world,
    evidence,
    read_only,
    remove,
)
from tests.utils.artifact_snapshot import assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and signals"),
]

_TARGET_ROOTS = (".github", ".claude", ".cursor", ".agents")


def test_read_only_module_store_fails_before_any_target_write(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    agent = world.project / "packages/alpha/.apm/agents/alpha.agent.md"
    agent.write_text(agent.read_text(encoding="ascii") + "v2\n", encoding="ascii")
    lock = world.lockfile_bytes()
    before = [world.snapshot(root) for root in _TARGET_ROOTS]

    with read_only(world.project / "apm_modules"):
        result = world.install()

    assert result.returncode != 0, evidence(result)
    assert "Permission denied" in result.stdout + result.stderr, evidence(result)
    assert world.lockfile_bytes() == lock
    for snapshot, root in zip(before, _TARGET_ROOTS, strict=True):
        assert_unchanged(snapshot, world.snapshot(root))
    world.assert_converges()


def test_target_root_replaced_by_file_fails_without_writes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    cursor = world.project / ".cursor"
    remove(cursor)
    cursor.write_text("user notes\n", encoding="ascii")
    lock = world.lockfile_bytes()
    before = [world.snapshot(root) for root in (".github", ".claude", ".agents")]

    result = world.install()

    assert result.returncode != 0, evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    assert ".cursor is a file that blocks an APM-managed path" in output, output
    assert "Move it aside" in output, output
    assert "resolve" not in output.lower(), output
    assert cursor.read_text(encoding="ascii") == "user notes\n"
    assert world.lockfile_bytes() == lock
    for snapshot, root in zip(before, (".github", ".claude", ".agents"), strict=True):
        assert_unchanged(snapshot, world.snapshot(root))

    cursor.unlink()
    world.assert_converges()


@pytest.mark.parametrize("obstruction", ["directory", "symlink"])
def test_obstructed_deployed_path_keeps_user_bytes(
    tmp_path: Path, apm_binary_path: Path, obstruction: str
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    deployed = world.project / ".claude/rules/alpha.md"
    linked = world.isolated.work_root / "linked.md"
    linked.write_text("unrelated content\n", encoding="ascii")
    deployed.unlink()
    if obstruction == "directory":
        deployed.mkdir()
        (deployed / "notes.md").write_text("user notes\n", encoding="ascii")
    else:
        deployed.symlink_to(linked)
    lock = world.lockfile_bytes()

    for command in (("install", "--no-policy"), ("install", "--no-policy", "--force")):
        result = world.apm(*command)
        assert result.returncode != 0, evidence(result)
        output = " ".join((result.stdout + result.stderr).split())
        if obstruction == "directory":
            blocked = ".claude/rules/alpha.md is a directory that blocks an APM-managed path"
            assert blocked in output, output
            assert "Move it aside" in output, output
            assert "Errno" not in output, output
        else:
            assert "alpha" in output, output
        assert world.lockfile_bytes() == lock
        assert linked.read_text(encoding="ascii") == "unrelated content\n"
        if obstruction == "directory":
            assert (deployed / "notes.md").read_text(encoding="ascii") == "user notes\n"
        else:
            assert deployed.is_symlink()

    remove(deployed)
    world.assert_converges()


def test_compile_output_write_failure_keeps_every_output(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path)
    source = world.project / "src"
    (source / "nested").mkdir(parents=True)
    (source / "app.py").write_text("print('app')\n", encoding="ascii")
    (source / "nested/AGENTS.md").write_text("user nested notes\n", encoding="ascii")
    first = world.apm("compile")
    assert first.returncode == 0, evidence(first)
    rule = world.project / "apm_modules/_local/alpha/.apm/instructions/alpha.instructions.md"
    rule.write_text(rule.read_text(encoding="ascii") + "v2 rule\n", encoding="ascii")
    before = world.snapshot(".")

    with read_only(source):
        result = world.apm("compile")

    assert result.returncode != 0, evidence(result)
    output = result.stdout + result.stderr
    assert output.count("Failed to write distributed output batch") == 1, output
    after = world.snapshot(".")
    diff = before.diff(after)
    assert not (diff.added | diff.removed | diff.changed), diff
    assert (source / "nested/AGENTS.md").read_text(encoding="ascii") == "user nested notes\n"


def test_interrupted_fresh_install_rolls_back_manifest_lock_and_staging(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    packages = tuple(f"pkg{index:02d}" for index in range(12))
    world = build_world(tmp_path, apm_binary_path, packages=packages, skills=8, installed=False)
    manifest = (world.project / "apm.yml").read_bytes()
    skills_root = world.project / ".claude/skills"
    process = subprocess.Popen(
        (str(apm_binary_path), "install", "--no-policy"),
        cwd=world.project,
        env=world.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 240
    while process.poll() is None and not any(skills_root.glob("*/SKILL.md")):
        assert time.monotonic() < deadline, "install never deployed a skill"
        time.sleep(0.01)
    assert process.poll() is None, "install finished before the interrupt"
    os.killpg(process.pid, signal.SIGINT)
    output, _ = process.communicate(timeout=120)

    assert process.returncode != 0, output
    assert "interrupted" in output.lower(), output
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert world.lockfile_bytes() is None
    modules = world.project / "apm_modules"
    assert not (modules / ".apm-resolution-staging").exists()
    assert not list(world.project.glob("apm-atomic-*"))
    world.assert_user_file_intact()
