from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_type_hints

import pytest

from tests.utils import scenario_rows
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.scenario_rows import (
    LifecycleAction,
    ScenarioObservation,
    ScenarioRow,
)


def _public_callable_names(value: object) -> set[str]:
    return {
        name for name in dir(value) if not name.startswith("_") and callable(getattr(value, name))
    }


def test_row_is_frozen_plain_data(tmp_path: Path) -> None:
    source_input = tmp_path / "source"
    action = LifecycleAction(("install", "--target", "copilot"))
    observation = ScenarioObservation(
        source_inputs=(source_input,),
        results=(),
        snapshots=(),
    )
    row = ScenarioRow(
        id="bare-skill",
        source_inputs=observation.source_inputs,
        lifecycle_actions=(action,),
    )

    assert row.id == "bare-skill"
    assert row.source_inputs == (source_input,)
    assert row.lifecycle_actions == (action,)
    assert row.lifecycle_actions[0].expected_returncode == 0

    with pytest.raises(FrozenInstanceError):
        row.id = "changed"
    with pytest.raises(FrozenInstanceError):
        action.expected_returncode = 1
    with pytest.raises(FrozenInstanceError):
        observation.source_inputs = ()


def test_row_records_and_module_expose_only_reviewed_contract() -> None:
    assert tuple(field.name for field in fields(LifecycleAction)) == (
        "args",
        "expected_returncode",
    )
    assert tuple(field.name for field in fields(ScenarioObservation)) == (
        "source_inputs",
        "results",
        "snapshots",
    )
    assert tuple(field.name for field in fields(ScenarioRow)) == (
        "id",
        "source_inputs",
        "lifecycle_actions",
    )

    assert get_type_hints(ScenarioObservation) == {
        "source_inputs": tuple[Path, ...],
        "results": tuple[CommandResult, ...],
        "snapshots": tuple[ArtifactSnapshot, ...],
    }

    allowed_public_surface = {
        "LifecycleAction",
        "ScenarioObservation",
        "ScenarioRow",
    }
    public_surface = {name for name in vars(scenario_rows) if not name.startswith("_")}
    public_callables = {
        name
        for name, value in vars(scenario_rows).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_surface == allowed_public_surface
    assert public_callables == allowed_public_surface


def test_each_record_exposes_only_reviewed_callable_surface(tmp_path: Path) -> None:
    records = (
        LifecycleAction(("install",)),
        ScenarioObservation(
            source_inputs=(tmp_path,),
            results=(),
            snapshots=(),
        ),
        ScenarioRow(
            id="plain",
            source_inputs=(tmp_path,),
            lifecycle_actions=(),
        ),
    )
    allowed_public_callables = {
        LifecycleAction: set(),
        ScenarioObservation: set(),
        ScenarioRow: set(),
    }

    for record in records:
        assert _public_callable_names(record) == allowed_public_callables[type(record)]
