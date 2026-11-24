"""The whole-plan overwrite preflight keeps what it found when planning fails (#4381)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.install import commit_gate
from apm_cli.integration import write_seam

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-ownership.preserve_unowned"),
]


class _Prompts:
    """A prompt integrator whose plan finds one unproven file, then fails."""

    _overwrite_plan: list | None = None
    _overwrite_gate = None

    def __init__(self, found: Path, error: BaseException) -> None:
        self.found = found
        self.error = error

    def integrate_prompts_for_target(self, *_args, **_kwargs) -> None:
        assert self._overwrite_plan is not None
        self._overwrite_plan.append((self.found, None))
        raise self.error


def _refuse(found: Path, error: BaseException) -> SimpleNamespace:
    """Run the commit gate's refusal over one package whose plan fails after a find."""
    from apm_cli.utils.diagnostics import DiagnosticCollector

    target = SimpleNamespace(name="copilot", primitives={"prompts": object()})
    ctx = SimpleNamespace(
        integrators={"prompt": _Prompts(found, error), "skill": None},
        project_root=found.parent,
        force=False,
        scope=None,
        targets=[],
        diagnostics=DiagnosticCollector(),
        overwrite_preflight_refused=False,
    )
    ctx.integrators["skill"] = SimpleNamespace(_materialization_session=object())
    plan = SimpleNamespace(
        source_plan=object(),
        codex_native=None,
        target_selection=SimpleNamespace(targets=[target]),
        projection_targets=[target],
    )
    package = SimpleNamespace(dependency_ref=None)
    with patch("apm_cli.copilot_plugins.capability.admits_native_plugin", return_value=False):
        commit_gate._refuse_unproven_overwrites(ctx, [(package, plan, None)])
    return ctx


def test_a_planning_failure_still_refuses_the_unproven_files_already_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _refuse(tmp_path / "review.prompt.md", RuntimeError("render failed"))
    ctx.diagnostics.render_summary()

    captured = capsys.readouterr()
    output = " ".join((captured.out + captured.err).split())
    assert ctx.overwrite_preflight_refused is True
    assert "Kept review.prompt.md" in output, output


def test_a_planned_write_is_never_swallowed(tmp_path: Path) -> None:
    with pytest.raises(write_seam.PlannedWriteError):
        _refuse(tmp_path / "review.prompt.md", write_seam.PlannedWriteError("wrote"))


class _Planner:
    """Every gated integrator: records each (primitive, target) it plans, finds *found*."""

    _overwrite_plan: list | None = None
    _overwrite_gate = SimpleNamespace(owner="pkg")

    def __init__(self, found: Path | None) -> None:
        self.found = found
        self.planned: list[str] = []

    def __getattr__(self, name: str):
        if not name.startswith("integrate_"):
            raise AttributeError(name)

        def plan(target, *_args, **_kwargs) -> None:
            assert self._overwrite_plan is not None
            self.planned.append(target.name)
            if self.found is not None and self.found.exists():
                self._overwrite_plan.append((self.found, None))

        return plan


def _gate(project: Path, plan_targets: list[str], found: Path | None = None) -> SimpleNamespace:
    """Run the refusal over one package that deploys to *plan_targets* (real profiles)."""
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    targets = [KNOWN_TARGETS[name] for name in plan_targets]
    planner = _Planner(found)
    ctx = SimpleNamespace(
        integrators={
            **dict.fromkeys(("prompt", "agent", "command", "instruction"), planner),
            "skill": SimpleNamespace(_materialization_session=object()),
        },
        project_root=project,
        force=False,
        scope=None,
        # The project's own targets: the proof must follow the package's selection.
        targets=[KNOWN_TARGETS["copilot"]],
        diagnostics=DiagnosticCollector(),
        overwrite_preflight_refused=None,
        planner=planner,
    )
    plan = SimpleNamespace(
        source_plan=object(),
        codex_native=None,
        target_selection=SimpleNamespace(targets=targets),
        projection_targets=targets,
    )
    package = SimpleNamespace(dependency_ref=None)
    with patch("apm_cli.copilot_plugins.capability.admits_native_plugin", return_value=False):
        commit_gate._refuse_unproven_overwrites(ctx, [(package, plan, None)])
    return ctx


def test_a_pre_existing_gated_file_still_runs_the_planning_pass(tmp_path: Path) -> None:
    found = tmp_path / ".github" / "prompts" / "review.prompt.md"
    found.parent.mkdir(parents=True)
    found.write_text("mine\n", encoding="utf-8")

    ctx = _gate(tmp_path, ["copilot"], found)

    assert "copilot" in ctx.planner.planned
    assert ctx.overwrite_preflight_refused is True


def test_empty_gated_dest_dirs_skip_the_planning_pass(tmp_path: Path) -> None:
    (tmp_path / ".github" / "instructions").mkdir(parents=True)
    # Ungated dirs may hold files: only gated primitives meet OverwriteGate.
    (tmp_path / ".github" / "hooks").mkdir()
    (tmp_path / ".github" / "hooks" / "mine.json").write_text("{}\n", encoding="utf-8")

    ctx = _gate(tmp_path, ["copilot"])

    assert ctx.planner.planned == []
    assert ctx.overwrite_preflight_refused is False


def test_the_proof_follows_the_package_targets_not_the_projects(tmp_path: Path) -> None:
    # A file under the project's copilot target the package never deploys to.
    mine = tmp_path / ".github" / "prompts" / "review.prompt.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine\n", encoding="utf-8")

    skipped = _gate(tmp_path, ["claude"])
    assert skipped.planner.planned == []
    assert skipped.overwrite_preflight_refused is False

    found = tmp_path / ".claude" / "agents" / "reviewer.md"
    found.parent.mkdir(parents=True)
    found.write_text("mine\n", encoding="utf-8")
    planned = _gate(tmp_path, ["claude"], found)
    assert "claude" in planned.planner.planned
    assert planned.overwrite_preflight_refused is True


def test_user_scope_always_runs_the_planning_pass(tmp_path: Path) -> None:
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.targets import KNOWN_TARGETS

    ctx = SimpleNamespace(scope=InstallScope.USER, project_root=tmp_path)
    plan = SimpleNamespace(target_selection=SimpleNamespace(targets=[KNOWN_TARGETS["copilot"]]))

    assert commit_gate._gated_dest_dirs_empty(ctx, [(None, plan, None)]) is False
