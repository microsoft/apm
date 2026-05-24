"""Regression test for #1411: drift replay must pre-create target dirs."""

from unittest.mock import MagicMock

import yaml

from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import DiagnosticCollector


def test_auto_create_false_targets_still_integrate_in_scratch(tmp_path):
    """Non-skill primitives must integrate even when auto_create=False.

    The drift replay engine writes into an empty scratch dir.
    Without pre-creating target root dirs, integrators with
    auto_create=False skip all non-skill primitives (#1411).
    """
    # Pick a target with auto_create=False (e.g. windsurf)
    windsurf = next(t for t in KNOWN_TARGETS.values() if t.name == "windsurf")
    assert not windsurf.auto_create, "windsurf should have auto_create=False"

    # Set up a minimal package with one instruction
    pkg_dir = tmp_path / "pkg"
    apm_dir = pkg_dir / ".apm" / "instructions"
    apm_dir.mkdir(parents=True)
    (apm_dir / "foo.instructions.md").write_text("# Hello\n", encoding="utf-8")

    package_info = MagicMock()
    package_info.install_path = pkg_dir

    # Scenario 1: scratch dir WITHOUT pre-created target root -> integration skips
    scratch_no_root = tmp_path / "scratch_no_root"
    scratch_no_root.mkdir()

    integrator = InstructionIntegrator()
    diag = DiagnosticCollector()
    result_no_root = integrator.integrate_instructions_for_target(
        windsurf,
        package_info,
        scratch_no_root,
        force=True,
        managed_files=set(),
        diagnostics=diag,
    )
    assert result_no_root.files_integrated == 0, (
        "Without target root dir, auto_create=False should skip"
    )

    # Scenario 2: scratch dir WITH pre-created target root -> integration succeeds
    scratch_with_root = tmp_path / "scratch_with_root"
    scratch_with_root.mkdir()
    (scratch_with_root / windsurf.root_dir).mkdir(parents=True, exist_ok=True)

    result_with_root = integrator.integrate_instructions_for_target(
        windsurf,
        package_info,
        scratch_with_root,
        force=True,
        managed_files=set(),
        diagnostics=diag,
    )
    assert result_with_root.files_integrated > 0, (
        "With target root dir pre-created, instructions should integrate"
    )


def test_run_replay_precreates_scratch_target_dirs(tmp_path):
    """run_replay must pre-create target root dirs in scratch for auto_create=False targets.

    Uses an empty lockfile so no packages are replayed — this isolates the
    pre-creation loop from dependency-download complexity. The assertion that
    ``.windsurf/`` exists in scratch proves the fix in drift.py fires before
    the integration loop, not just as a side-effect of integrating files.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.drift import CheckLogger, ReplayConfig, run_replay

    # Minimal project targeting windsurf (auto_create=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.dump({"name": "test", "version": "1.0.0", "target": "windsurf"}),
        encoding="utf-8",
    )

    # Empty lockfile — no deps, so the only effect visible in scratch is the
    # pre-created target root directory planted by the fix.
    lock_path = project / "apm.lock.yaml"
    LockFile().write(lock_path)

    config = ReplayConfig(project_root=project, lockfile_path=lock_path, cache_only=True)
    logger = CheckLogger(verbose=False)

    scratch = run_replay(config, logger)

    windsurf = next(t for t in KNOWN_TARGETS.values() if t.name == "windsurf")
    assert (scratch / windsurf.root_dir).is_dir(), (
        f"run_replay must pre-create {windsurf.root_dir!r} in scratch "
        "for auto_create=False targets (regression for #1411)"
    )
