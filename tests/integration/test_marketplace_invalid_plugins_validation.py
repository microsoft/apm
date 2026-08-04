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


@pytest.mark.parametrize(
    ("case_name", "source_bytes", "expected_error"),
    [
        (
            "plugins-not-list",
            b'{\n  "name": "malformed",\n  "plugins": "not-an-array"\n}\n',
            "plugins: expected a list",
        ),
        (
            "empty-source",
            b'{\n  "name": "malformed",\n  "plugins": [{"name": "bad", "source": {}}]\n}\n',
            "plugins[0].source: expected a supported source type or an owner/repository field",
        ),
        (
            "unsupported-source",
            b'{\n  "name": "malformed",\n  "plugins": [{"name": "bad", "source": {"type": "bogus"}}]\n}\n',
            "plugins[0].source: unsupported source type 'bogus'",
        ),
        (
            "github-without-repo",
            b'{\n  "name": "malformed",\n  "plugins": [{"name": "bad", "source": {"type": "github"}}]\n}\n',
            "plugins[0].source: github requires an owner/repository field",
        ),
        (
            "local-github-source",
            b'{\n  "name": "malformed",\n  "plugins": [{"name": "bad", "source": {"type": "github", "repo": "/tmp/local-plugin"}}]\n}\n',
            "plugins[0].source: github requires a non-local owner/repository field",
        ),
    ],
)
def test_marketplace_invalid_plugins_validation(
    tmp_path: Path,
    apm_binary_path: Path,
    case_name: str,
    source_bytes: bytes,
    expected_error: str,
) -> None:
    """Registration tolerates malformed plugins while validation reports its path."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    source = isolated.work_root / "malformed-marketplace"
    source.mkdir()
    manifest_path = source / "marketplace.json"
    manifest_path.write_bytes(source_bytes)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    add_result = runner.run(
        ("marketplace", "add", str(source), "--name", "malformed"),
        scenario_id=f"marketplace-invalid-plugins-validation-{case_name}",
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
    )
    registry_path = isolated.config_root / "marketplaces.json"
    registry_bytes = registry_path.read_bytes()
    (validate_result,) = runner.run_sequence(
        (("marketplace", "validate", "malformed"),),
        expected_returncodes=(1,),
        scenario_id=f"marketplace-invalid-plugins-validation-{case_name}",
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
    )

    assert add_result.returncode == 0
    add_output = add_result.stdout + add_result.stderr
    assert "contains 1 unsupported or malformed plugin entry" in add_output
    validation_output = validate_result.stdout + validate_result.stderr
    assert " ".join(expected_error.split()) in " ".join(validation_output.split())
    assert "Found 0 plugins" not in validation_output
    assert "Schema: passed" not in validation_output
    assert "Names: passed" not in validation_output
    assert manifest_path.read_bytes() == source_bytes
    assert registry_path.read_bytes() == registry_bytes
