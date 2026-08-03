"""Lifecycle coverage for validating a tolerantly registered malformed marketplace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_marketplace_invalid_plugins_validation(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Registration tolerates malformed plugins while validation reports its path."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    source = isolated.work_root / "malformed-marketplace"
    source.mkdir()
    manifest_path = source / "marketplace.json"
    source_bytes = b'{\n  "name": "malformed",\n  "plugins": "not-an-array"\n}\n'
    manifest_path.write_bytes(source_bytes)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    add_result = runner.run(
        ("marketplace", "add", str(source), "--name", "malformed"),
        scenario_id="marketplace-invalid-plugins-validation",
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
    )
    registry_path = isolated.config_root / "marketplaces.json"
    registry_bytes = registry_path.read_bytes()
    (validate_result,) = runner.run_sequence(
        (("marketplace", "validate", "malformed"),),
        expected_returncodes=(1,),
        scenario_id="marketplace-invalid-plugins-validation",
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
    )

    assert add_result.returncode == 0
    validation_output = validate_result.stdout + validate_result.stderr
    assert "plugins" in validation_output
    assert "expected a list" in validation_output
    assert "Found 0 plugins" not in validation_output
    assert "Schema: passed" not in validation_output
    assert "Names: passed" not in validation_output
    assert manifest_path.read_bytes() == source_bytes
    assert registry_path.read_bytes() == registry_bytes
