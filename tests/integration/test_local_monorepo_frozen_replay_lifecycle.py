"""A monorepo lib reached through two relative spellings replays with --frozen (#4731).

PROMISE: lockfile.frozen_preserves_bytes -- ``apm install --frozen`` replays the
lockfile ``apm install`` just wrote, without changing the lockfile or the deployed
files (source: issue #4731 scope record).
CELL: OP=install FROZEN=on SRC=local(transitive, two spellings) PRIM=skill SCOPE=project
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I2", "P-INV-I3"),
    pytest.mark.requires_apm_binary,
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    _write(root / "apm.yml", manifest)
    _write(
        root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md",
        f"---\nname: {name}-skill\ndescription: {name} skill\n---\n{name} body\n",
    )


def _evidence(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def test_frozen_replays_a_lib_reached_by_two_relative_spellings(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
    root = isolated.work_root / "monorepo"
    packages = root / "packages"
    _package(packages / "lib-06", "lib-06")
    _package(packages / "lib-05", "lib-05", ("../lib-06",))
    _package(packages / "lib-04", "lib-04", ("../lib-05",))
    _write(
        root / "apm.yml",
        "name: monorepo\nversion: 1.0.0\ntargets: [claude]\n"
        "dependencies:\n  apm:\n    - ./packages/lib-04\n    - ./packages/lib-05\n",
    )
    (root / ".claude").mkdir()

    installed = runner.run(("install",), cwd=root, env=env)
    assert installed.returncode == 0, _evidence(installed)
    lock = (root / "apm.lock.yaml").read_bytes()
    deployed = ArtifactSnapshot.capture(root / ".claude")
    for name in ("lib-04", "lib-05", "lib-06"):
        assert (root / ".claude" / "skills" / f"{name}-skill" / "SKILL.md").is_file()

    frozen = runner.run(("install", "--frozen"), cwd=root, env=env)

    assert frozen.returncode == 0, _evidence(frozen)
    assert "ambiguous resolved_by parent" not in frozen.stdout + frozen.stderr
    assert (root / "apm.lock.yaml").read_bytes() == lock
    assert_unchanged(deployed, ArtifactSnapshot.capture(root / ".claude"))
    # Audit's drift replay resolves the same parents; the duplicate lock row's
    # skill-subset verdict is tracked separately (not this fix's owner).
    audit = runner.run(("audit", "--ci"), cwd=root, env=env)
    assert "ambiguous resolved_by parent" not in audit.stdout + audit.stderr, _evidence(audit)
