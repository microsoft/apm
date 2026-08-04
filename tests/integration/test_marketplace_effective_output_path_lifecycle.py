"""Installed-binary lifecycle contract for marketplace output-path overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_ARTIFACT = ".github/plugin/marketplace.json"
_DEFAULT_ARTIFACT = ".claude-plugin/marketplace.json"


def _result_evidence(result: CommandResult) -> str:
    """Return captured process evidence for a failed lifecycle assertion."""
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _write_project(project_root: Path) -> None:
    """Create a local-only marketplace project with no network dependency."""
    (project_root / ".github/plugins/local-tool").mkdir(parents=True)
    (project_root / "apm.yml").write_text(
        """\
name: output-path-lifecycle
description: Marketplace output path lifecycle fixture
version: 1.0.0
marketplace:
  owner:
    name: APM Lifecycle Tests
  packages:
    - name: local-tool
      source: ./.github/plugins/local-tool
      description: Local fixture
      version: 1.0.0
""",
        encoding="utf-8",
    )


def test_marketplace_override_clean_check_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Pack and --check-clean must share one overridden artifact path."""
    project_root = tmp_path / "marketplace-override-clean-check"
    project_root.mkdir()
    _write_project(project_root)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    environment = dict(os.environ)
    override = f"claude={_ARTIFACT}"

    packed = runner.run(
        ("pack", "--offline", "--marketplace-path", override),
        scenario_id="marketplace-override-clean-check-pack",
        cwd=project_root,
        env=environment,
    )
    assert packed.returncode == 0, _result_evidence(packed)
    artifact = project_root / _ARTIFACT
    assert artifact.is_file()
    initial_bytes = artifact.read_bytes()
    assert not (project_root / _DEFAULT_ARTIFACT).exists()

    checked = runner.run(
        ("pack", "--offline", "--marketplace-path", override, "--check-clean"),
        scenario_id="marketplace-override-clean-check",
        cwd=project_root,
        env=environment,
    )
    assert checked.returncode == 0, _result_evidence(checked)
    assert f"{_ARTIFACT}  [unchanged]" in (checked.stdout + checked.stderr)
    assert artifact.read_bytes() == initial_bytes
    assert not (project_root / _DEFAULT_ARTIFACT).exists()
