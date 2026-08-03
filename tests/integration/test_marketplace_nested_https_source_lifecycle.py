"""Installed-binary lifecycle contract for nested HTTPS marketplace sources."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_NESTED_SOURCE = "https://git.example.invalid/group/subgroup/marketplace-package.git"


def test_marketplace_check_offline_reaches_nested_https_ref_resolution_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A nested HTTPS source reaches offline resolution without project mutation."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "nested-marketplace"
    project.mkdir()
    (project / "apm.yml").write_text(
        f"""\
name: nested-marketplace
description: Nested HTTPS marketplace source lifecycle test
version: 1.0.0
marketplace:
  owner:
    name: Test Owner
  packages:
    - name: nested-package
      source: {_NESTED_SOURCE}
      ref: v1.0.0
""",
        encoding="utf-8",
    )
    before = ArtifactSnapshot.capture(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    (result,) = runner.run_sequence(
        (("marketplace", "check", "--offline", "--verbose"),),
        expected_returncodes=(1,),
        scenario_id="marketplace-nested-https-check",
        cwd=project,
        env=isolated.subprocess_env(overrides={"COLUMNS": "240"}),
    )

    diagnostics = result.stdout + result.stderr
    assert "marketplace config error" not in diagnostics
    assert "No cached refs (offline)" in diagnostics
    assert _NESTED_SOURCE in diagnostics
    assert_unchanged(before, ArtifactSnapshot.capture(project))
