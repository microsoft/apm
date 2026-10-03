"""Real source-installed CLI coverage for GitHub prune casing."""

import os

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.helpers.prune_casing import _project
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("dry_run", [True, False])
def test_prune_casing_real_cli(tmp_path, dry_run, apm_engine_command):
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root
    package, orphan = _project(project, "Microsoft/APM", "microsoft/apm", with_lock=True)
    result = ApmLifecycleRunner(apm_engine_command, timeout_seconds=30).run(
        ("prune", *(["--dry-run"] if dry_run else [])),
        scenario_id=f"prune-github-casing-{dry_run}",
        cwd=project,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if dry_run:
        assert "microsoft/apm" not in result.stdout
    assert (package / "notes.txt").read_bytes() == b"user content\n"
    assert orphan.exists() is dry_run
    assert "microsoft/apm" in LockFile.read(project / "apm.lock.yaml").dependencies
