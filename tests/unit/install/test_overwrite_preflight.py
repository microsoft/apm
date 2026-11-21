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


def _plan(found: Path, error: BaseException) -> list:
    target = SimpleNamespace(name="copilot", primitives={"prompts": object()})
    ctx = SimpleNamespace(
        integrators={"prompt": _Prompts(found, error)},
        project_root=found.parent,
        force=False,
        scope=None,
    )
    plan = SimpleNamespace(
        source_plan=object(),
        codex_native=None,
        target_selection=SimpleNamespace(targets=[target]),
        projection_targets=[target],
    )
    skill = SimpleNamespace(_materialization_session=None)
    package = SimpleNamespace(dependency_ref=None)
    with patch("apm_cli.copilot_plugins.capability.admits_native_plugin", return_value=False):
        return commit_gate._planned_keeps(ctx, skill, package, plan, None)


def test_a_planning_failure_keeps_the_unproven_files_already_found(tmp_path: Path) -> None:
    found = tmp_path / "review.prompt.md"

    kept = _plan(found, RuntimeError("render failed"))

    assert kept == [(".", found, None)]


def test_a_planned_write_is_never_swallowed(tmp_path: Path) -> None:
    with pytest.raises(write_seam.PlannedWriteError):
        _plan(tmp_path / "review.prompt.md", write_seam.PlannedWriteError("wrote"))
