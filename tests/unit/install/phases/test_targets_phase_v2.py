"""Unit tests for the v2 targets phase three-guard collapse (#1154).

These tests EXTEND tests/unit/install/phases/test_targets_phase.py and
defend the three-guard collapse: the resolved-target path always
materializes deploy directories. There is no silent skip.

# Implementation contract assumed by these tests (TDD-red):
#
# Module: apm_cli.install.phases.targets
#   - run_targets_phase(ctx) -> None
#       After this call, every target in ctx.targets has a TargetProfile
#       with .resolved_deploy_root pointing at an existing directory.
#       Auto-detected targets behave the same as explicit targets
#       (auto_create=True); no _explicit guard remains.
#
# We mirror the ctx-mock pattern from the existing
# test_targets_phase.py so the two suites can coexist.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope


def _make_ctx(
    project_root: Path,
    *,
    target_override: str | None = None,
    yaml_target: str | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.project_root = project_root
    project_root.mkdir(parents=True, exist_ok=True)
    ctx.scope = InstallScope.PROJECT
    ctx.target_override = target_override
    ctx.apm_package = MagicMock()
    ctx.apm_package.target = yaml_target
    ctx.logger = MagicMock()
    ctx.targets = []
    ctx.integrators = {}
    return ctx


def _target_root_dirs(ctx, project_root: Path) -> list[Path]:
    """Collect on-disk deploy directories for every TargetProfile in ctx.targets.

    Static targets resolve to ``project_root / target.root_dir`` (e.g.
    ``.claude``). Dynamic targets (cowork) carry an explicit
    ``resolved_deploy_root`` -- if present, prefer it.
    """
    out: list[Path] = []
    for t in ctx.targets:
        root = getattr(t, "resolved_deploy_root", None)
        if root is not None:
            out.append(Path(root))
            continue
        root_dir = getattr(t, "root_dir", None)
        if root_dir:
            out.append(project_root / root_dir)
    return out


def test_three_guard_collapse_no_skip(tmp_path):
    """Auto-detected target is never silently skipped after resolution."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_bytes(b"# Claude\n")

    ctx = _make_ctx(project)
    run_targets_phase(ctx)

    assert ctx.targets, "run_targets_phase produced no targets"
    dirs = _target_root_dirs(ctx, project)
    assert any(d.name == ".claude" for d in dirs), f"No .claude/ deploy dir resolved; got {dirs}"


def test_explicit_creates_missing_dir(tmp_path):
    """--target claude in greenfield creates .claude/ before phase exits."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, target_override="claude")
    run_targets_phase(ctx)

    assert (project / ".claude").is_dir(), "Explicit --target claude must materialize .claude/"


@pytest.mark.parametrize(
    ("marker_path", "expected_dir"),
    [
        ("CLAUDE.md", ".claude"),
        ("GEMINI.md", ".gemini"),
    ],
    ids=["claude_md_only", "gemini_md_only"],
)
def test_auto_detect_creates_dir_for_resolved(tmp_path, marker_path, expected_dir):
    """Auto-detected marker without companion dir still materializes it."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()
    (project / marker_path).write_bytes(b"# Marker\n")

    ctx = _make_ctx(project)
    run_targets_phase(ctx)

    assert (project / expected_dir).is_dir(), (
        f"Auto-detected {marker_path} must materialize {expected_dir}/"
    )
