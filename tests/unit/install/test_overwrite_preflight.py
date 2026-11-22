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
