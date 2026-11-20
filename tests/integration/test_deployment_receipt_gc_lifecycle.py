"""Installed-CLI proof: orphaned deployment receipts are collected (#4176).

Each project's deployment receipt lives under
``$APM_HOME/deployment-receipts/<key>.json``. When the project root it
records no longer exists, the next ``apm install`` or ``apm prune`` removes
it, best effort; a live project's receipt always survives.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_RULE = "---\napplyTo: '**'\ndescription: Receipt rule\n---\nKeep receipts tidy.\n"


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _receipts(apm_home: Path) -> dict[str, Path]:
    """Recorded project root -> receipt file."""
    directory = apm_home / "deployment-receipts"
    return {
        json.loads(path.read_text(encoding="utf-8"))["root"]: path
        for path in sorted(directory.glob("*.json"))
    }


def _project(packages: LocalPackageFactory, name: str) -> LocalPackage:
    project = packages.create(name, targets=("copilot",))
    packages.add_instruction(project, f"{name}-rule", _RULE)
    return project


@pytest.mark.parametrize("command", ["install", "prune"])
def test_orphaned_receipt_is_collected_and_live_receipt_survives(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / command, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)
    live = _project(packages, "live-project")
    gone = _project(packages, "gone-project")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def run(project: LocalPackage, *args: str) -> CommandResult:
        result = runner.run(args, cwd=project.root, env=env, scenario_id=f"receipts-{args[0]}")
        assert result.returncode == 0, _evidence(result)
        return result

    run(live, "install")
    run(gone, "install")
    receipts = _receipts(isolated.config_root)
    live_root = live.root.resolve().as_posix()
    gone_root = gone.root.resolve().as_posix()
    assert {live_root, gone_root} <= set(receipts), receipts
    live_receipt = receipts[live_root].read_bytes()
    stray = isolated.config_root / "deployment-receipts" / "notes.txt"
    stray.write_text("user file\n", encoding="utf-8")

    shutil.rmtree(gone.root)
    run(live, command)

    after = _receipts(isolated.config_root)
    assert gone_root not in after, after
    assert after[live_root].read_bytes() == live_receipt
    assert stray.read_text(encoding="utf-8") == "user file\n"


def test_collection_reaches_an_orphan_behind_a_full_batch_of_retained_receipts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Collection resumes where the previous command stopped, so every receipt is visited."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "batch", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)
    live = _project(packages, "live-project")
    gone = _project(packages, "gone-project")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def install(project: LocalPackage, label: str) -> None:
        result = runner.run(("install",), cwd=project.root, env=env, scenario_id=label)
        assert result.returncode == 0, _evidence(result)

    install(live, "batch-live")
    install(gone, "batch-gone")
    directory = isolated.config_root / "deployment-receipts"
    gone_root = gone.root.resolve().as_posix()
    assert gone_root in _receipts(isolated.config_root)
    retained = [directory / f"{index:04d}-retained.json" for index in range(300)]
    for path in retained:
        path.write_text("{}\n", encoding="utf-8")
    shutil.rmtree(gone.root)

    for attempt in range(3):
        install(live, f"batch-collect-{attempt}")
        if not any(
            json.loads(path.read_text(encoding="utf-8")).get("root") == gone_root
            for path in directory.glob("*.json")
        ):
            break
    else:
        pytest.fail("the orphaned receipt behind 300 retained receipts was never collected")
    assert all(path.read_text(encoding="utf-8") == "{}\n" for path in retained)
