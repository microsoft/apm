"""Installed-CLI lifecycle coverage for bounded YAML graph accounting."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import _MAX_YAML_INPUT_BYTES, _BoundedSafeLoader
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.yaml_guard_fixtures import (
    PRIVATE_LOCKFILE_MARKER,
    compact_alias_bomb_lockfile,
    large_anchor_free_lockfile,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--no-drift", "--format", "json")


def _project(root: Path) -> tuple[Path, dict[str, str]]:
    """Create one hermetic dependency-free project and child environment."""
    isolated = IsolatedApmEnvironment.create(root, base_env=os.environ)
    project = isolated.work_root / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: yaml-guard-lifecycle\nversion: 0.1.0\ntarget: copilot\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    return project, isolated.subprocess_env(overrides={"APM_DISABLE_UPDATE_CHECK": "1"})


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return the bounded installed-binary runner for this lifecycle."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=30,
        scenario_timeout_seconds=60,
    )


def test_large_anchor_free_lockfile_audits_without_rewrite(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed audit accepts a large literal lockfile byte-for-byte."""
    project, environment = _project(tmp_path / "large-valid")
    lockfile = project / "apm.lock.yaml"
    content = large_anchor_free_lockfile(_BoundedSafeLoader._MAX_EXPANSION_WEIGHT)
    assert len(content.encode("utf-8")) < 8_000_000
    lockfile.write_text(content, encoding="utf-8")
    before_bytes = lockfile.read_bytes()
    before_stat = lockfile.stat()

    result = _runner(apm_binary_path).run(
        _AUDIT_ARGS,
        scenario_id="large-anchor-free-lockfile",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    assert json.loads(result.stdout)["passed"] is True
    after_stat = lockfile.stat()
    assert lockfile.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_ino == before_stat.st_ino


def test_alias_bomb_lockfile_fails_audit_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed audit rejects a compact alias graph without side effects."""
    project, environment = _project(tmp_path / "alias-bomb")
    lockfile = project / "apm.lock.yaml"
    content = compact_alias_bomb_lockfile()
    assert len(content.encode("utf-8")) < 4096
    lockfile.write_text(content, encoding="utf-8")
    before = ArtifactSnapshot.capture(project)

    result = _runner(apm_binary_path).run(
        _AUDIT_ARGS,
        scenario_id="alias-bomb-lockfile",
        cwd=project,
        env=environment,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "YAML alias/anchor expansion exceeded the safe budget" in combined_output
    assert PRIVATE_LOCKFILE_MARKER not in combined_output
    assert_unchanged(before, ArtifactSnapshot.capture(project))


def test_oversize_literal_lockfile_fails_audit_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An oversized literal lockfile fails safely with a recovery action."""
    project, environment = _project(tmp_path / "oversize-literal")
    lockfile = project / "apm.lock.yaml"
    content = large_anchor_free_lockfile(_MAX_YAML_INPUT_BYTES)
    assert len(content.encode("utf-8")) > _MAX_YAML_INPUT_BYTES
    lockfile.write_text(content, encoding="utf-8")
    before = ArtifactSnapshot.capture(project)

    result = _runner(apm_binary_path).run(
        _AUDIT_ARGS,
        scenario_id="oversize-literal-lockfile",
        cwd=project,
        env=environment,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "YAML input exceeds" in combined_output
    assert "reduce or regenerate the YAML file before retrying" in combined_output
    assert_unchanged(before, ArtifactSnapshot.capture(project))
