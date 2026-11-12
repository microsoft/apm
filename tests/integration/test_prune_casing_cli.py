"""Real source-installed CLI coverage for GitHub prune casing."""

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.helpers.prune_casing import _prefix_project, _project, _snapshot
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke]


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


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("duplicate", [False, True], ids=["different-repos", "same-repo"])
def test_prune_complete_paths_on_case_sensitive_filesystem(
    tmp_path: Path, dry_run: bool, duplicate: bool, apm_engine_command: tuple[str, ...]
) -> None:
    """Required Linux CLI coverage uses real case-distinct directories, not mocks."""
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if (tmp_path / "caseprobe").exists():
        pytest.skip("Filesystem does not support distinct case-only directory names")
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root
    orphan, _ = _prefix_project(project, duplicate=duplicate)
    modules = project / "apm_modules"
    assert not (modules / "Microsoft").samefile(modules / "microsoft")
    before = _snapshot(modules)
    manifest = (project / "apm.yml").read_bytes()
    result = ApmLifecycleRunner(apm_engine_command, timeout_seconds=30).run(
        ("prune", *(["--dry-run"] if dry_run else [])),
        scenario_id=f"prune-complete-paths-{duplicate}-{dry_run}",
        cwd=project,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == (1 if duplicate else 0), result.stdout + result.stderr
    assert orphan.exists() is (dry_run or duplicate)
    expected = {
        name: content
        for name, content in before.items()
        if dry_run or duplicate or not name.startswith("other/orphan/")
    }
    assert _snapshot(modules) == expected
    assert (project / "apm.yml").read_bytes() == manifest
    if duplicate:
        assert "Found multiple package directories for one dependency:" in " ".join(
            result.stdout.split()
        )
