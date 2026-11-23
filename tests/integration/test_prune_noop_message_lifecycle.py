"""APMLifecycle: a no-op ``apm prune`` prints one message, whatever apm_modules/ holds (#4460).

Promise (I9): a command reports the same outcome with the same words. Before the
fix, a project with no apm_modules/ printed "No apm_modules/ directory found.
Nothing to prune." while a clean apm_modules/ printed "No orphaned packages
found. apm_modules/ is clean."; both are the same no-op.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I9", "P-OWN-user-facing-output-diagnostics"),
]

_NO_OP = "[+] No orphaned packages found. Nothing to prune."


@pytest.mark.parametrize("modules", ["absent", "empty", "hidden-records-only"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_noop_prune_prints_one_message(
    tmp_path: Path, apm_binary_path: Path, modules: str, dry_run: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    project = LocalPackageFactory(isolated.work_root).create("consumer", targets=("copilot",))
    apm_modules = project.root / "apm_modules"
    if modules != "absent":
        apm_modules.mkdir()
    if modules == "hidden-records-only":
        (apm_modules / ".apm-record").write_text("record\n", encoding="utf-8")
    before = ArtifactSnapshot.capture(project.root)

    args = ("prune", "--dry-run") if dry_run else ("prune",)
    result = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60).run(
        args, cwd=project.root, env=isolated.subprocess_env(), scenario_id=f"prune-noop-{modules}"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    assert lines.count(_NO_OP) == 1, result.stdout
    assert "Nothing to prune" not in result.stdout.replace(_NO_OP, ""), result.stdout
    assert "apm_modules/ is clean" not in result.stdout, result.stdout
    assert "No apm_modules/ directory found" not in result.stdout, result.stdout
    assert_unchanged(before, ArtifactSnapshot.capture(project.root))
