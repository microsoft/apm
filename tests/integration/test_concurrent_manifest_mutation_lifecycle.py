"""Installed-CLI contract: racing manifest mutations converge.

Two real ``apm`` processes mutate the same project at once -- one adds a
package, the other removes a different one. The lifecycle lock must
serialize them so the final state honours BOTH intents: apm.yml, the
lockfile and the deployed files agree, ``apm audit --ci`` is clean and a
rerun is a byte-for-byte no-op.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_PACKAGES = ("alpha", "beta", "gamma")


def _package(root: Path, name: str) -> None:
    rules = root / ".apm" / "instructions"
    rules.mkdir(parents=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    (rules / f"{name}.instructions.md").write_text(
        f'---\napplyTo: "**"\n---\n# {name} rule\n', encoding="ascii"
    )


def _deployed(project: Path) -> set[str]:
    return {path.name for path in (project / ".github" / "instructions").glob("*.md")}


def test_concurrent_install_and_uninstall_converge(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    for name in _PACKAGES:
        _package(project / "pkgs" / name, name)
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "target": "copilot",
            "dependencies": {"apm": ["./pkgs/alpha", "./pkgs/beta"]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _deployed(project) == {"alpha.instructions.md", "beta.instructions.md"}

    commands = (
        ("install", "--no-policy", "./pkgs/gamma"),
        ("uninstall", "./pkgs/beta"),
    )
    processes = [
        subprocess.Popen(
            [str(apm_binary_path), *args],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for args in commands
    ]
    try:
        outputs = [process.communicate(timeout=300) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    for args, process, (stdout, stderr) in zip(commands, processes, outputs, strict=True):
        assert process.returncode == 0, f"apm {' '.join(args)}\n{stdout}{stderr}"

    manifest = load_yaml(project / "apm.yml")
    assert manifest["dependencies"]["apm"] == ["./pkgs/alpha", "./pkgs/gamma"]
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    assert sorted(dep.local_path for dep in lock.dependencies.values()) == [
        "./pkgs/alpha",
        "./pkgs/gamma",
    ]
    assert _deployed(project) == {"alpha.instructions.md", "gamma.instructions.md"}

    audit = runner.run(("audit", "--ci", "--no-policy"), cwd=project, env=env)
    assert audit.returncode == 0, audit.stdout + audit.stderr

    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    manifest_bytes = (project / "apm.yml").read_bytes()
    rerun = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (project / "apm.yml").read_bytes() == manifest_bytes
