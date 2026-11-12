"""Existing frozen no-write obligations for admitted local single-file inputs."""

import os
from pathlib import Path

import pytest

from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize("suffix", ["agent", "instructions", "prompt"])
def test_frozen_local_file_requires_lock_before_any_durable_mutation(
    tmp_path: Path, apm_binary_path: Path, suffix: str
) -> None:
    """A real frozen invocation with no lock cannot bootstrap local-file state."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "frozen-file", base_env=os.environ)
    reference = f"./selected.{suffix}.md"
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=(reference,), targets=("copilot",)
    )
    source = project.root / f"selected.{suffix}.md"
    source.write_bytes(b"---\ndescription: Frozen no-write contract\n---\n# Guidance\n")
    existing = project.root / ".github" / "user.txt"
    existing.parent.mkdir()
    existing.write_bytes(b"User-owned bytes.\n")
    before = ArtifactSnapshot.capture(project.root)

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--frozen", "--target", "copilot", "--no-policy"),
        scenario_id=f"frozen-local-{suffix}-missing-lock",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    diagnostic = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, diagnostic
    assert "--frozen requires apm.lock.yaml to exist" in diagnostic, diagnostic
    assert_unchanged(before, ArtifactSnapshot.capture(project.root))
    assert not (project.root / "apm.lock.yaml").exists()
    assert not (project.root / "apm_modules").exists()
