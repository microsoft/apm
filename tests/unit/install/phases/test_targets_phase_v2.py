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
    target_override: str | list[str] | None = None,
    yaml_target: str | None = None,
    yaml_targets: list[str] | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.project_root = project_root
    project_root.mkdir(parents=True, exist_ok=True)
    ctx.scope = InstallScope.PROJECT
    ctx.target_override = target_override
    ctx.apm_package = MagicMock()
    ctx.apm_package.target = yaml_target
    if yaml_targets is not None:
        ctx.apm_package.targets = yaml_targets
    ctx.logger = MagicMock()
    ctx.targets = []
    ctx.integrators = {}
    ctx.legacy_skill_paths = False
    return ctx


def _target_names(ctx: MagicMock) -> list[str]:
    """Return the resolved target profile names from a phase context."""
    return [target.name for target in ctx.targets]


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


def test_runtime_alias_list_preserves_copilot_profile_and_dirs(tmp_path: Path) -> None:
    """Multi-target runtime aliases resolve through the copilot profile."""
    from apm_cli.install.phases.targets import run

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, target_override=["claude", "vscode"])
    run(ctx)

    assert _target_names(ctx) == ["claude", "copilot"]
    assert (project / ".claude").is_dir()
    assert (project / ".github").is_dir()


@pytest.mark.parametrize("target_override", [["vscode"], "vscode"])
def test_run_targets_phase_normalizes_vscode_alias(
    tmp_path: Path,
    target_override: str | list[str],
) -> None:
    """The v2 wrapper accepts vscode as the runtime alias for copilot."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, target_override=target_override)
    run_targets_phase(ctx)

    assert _target_names(ctx) == ["copilot"]
    assert (project / ".github").is_dir()


def test_cli_parse_claude_copilot_installs_both_targets(tmp_path: Path) -> None:
    """Regression trap for #1746: --target claude,copilot resolves both targets.

    parse_target_field intentionally returns the runtime alias spelling for
    multi-token input ("copilot" -> "vscode"); the targets phase must then
    normalize that alias back to the canonical "copilot" profile instead of
    silently dropping it.
    """
    from apm_cli.core.target_detection import parse_target_field
    from apm_cli.install.phases.targets import run

    project = tmp_path / "project"
    project.mkdir()

    parsed = parse_target_field("claude,copilot")
    ctx = _make_ctx(project, target_override=parsed)
    run(ctx)

    # Multi-token parsing yields the runtime alias, not the canonical name.
    assert parsed == ["claude", "vscode"]
    # The phase normalizes the alias so the copilot profile is preserved.
    assert _target_names(ctx) == ["claude", "copilot"]
    assert (project / ".claude").is_dir()
    assert (project / ".github").is_dir()


def test_run_targets_phase_dedupes_copilot_runtime_aliases(tmp_path: Path) -> None:
    """Mixed canonical/runtime tokens collapse to one copilot profile."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, target_override=["copilot", "vscode"])
    run_targets_phase(ctx)

    assert _target_names(ctx) == ["copilot"]


def test_experimental_target_override_skips_v2_resolver(tmp_path: Path) -> None:
    """Non-canonical experimental targets stay on the legacy-only path."""
    from apm_cli.install.phases.targets import _resolve_targets_by_scope
    from apm_cli.integration.targets import KNOWN_TARGETS

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, target_override="copilot-cowork")
    targets = _resolve_targets_by_scope(
        ctx,
        [KNOWN_TARGETS["copilot-cowork"]],
        "copilot-cowork",
        False,
    )

    assert [target.name for target in targets] == ["copilot-cowork"]


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


def test_plural_yaml_targets_attribute_creates_only_declared_dir(tmp_path):
    """targets: from the parsed APMPackage model drives v2 target selection."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, yaml_targets=["claude"])
    run_targets_phase(ctx)

    assert [target.name for target in ctx.targets] == ["claude"]
    assert (project / ".claude").is_dir()
    assert not (project / ".github").exists()


def test_run_targets_phase_conflicting_target_fields_exits_with_usage_code(tmp_path: Path) -> None:
    """run_targets_phase preserves usage-error exit code for target conflicts."""
    from apm_cli.install.phases.targets import run_targets_phase

    project = tmp_path / "project"
    project.mkdir()

    ctx = _make_ctx(project, yaml_target="claude", yaml_targets=["copilot"])

    with pytest.raises(SystemExit) as exc_info:
        run_targets_phase(ctx)

    assert exc_info.value.code == 2
    ctx.logger.error.assert_called_once()


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
