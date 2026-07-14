from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_args, get_origin

import pytest

from tests.utils import scenario_rows
from tests.utils.scenario_rows import (
    LifecycleAction,
    ScenarioAssertion,
    ScenarioObservation,
    ScenarioRow,
)


def _named_assertion(observation: object) -> None:
    assert observation is not None


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
        assertions=(_named_assertion,),
    )

    assert row.id == "bare-skill"
    assert row.source_inputs == (source_input,)
    assert row.lifecycle_actions == (action,)
    assert row.lifecycle_actions[0].expected_returncode == 0
    assert row.assertions[0].__name__ == "_named_assertion"
    row.assertions[0](observation)

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
        "assertions",
    )

    expected_assertion = Callable[[ScenarioObservation], None]
    assert ScenarioAssertion == expected_assertion
    assert get_origin(ScenarioAssertion) is Callable
    assert get_args(ScenarioAssertion) == ([ScenarioObservation], None)

    allowed_public_surface = {
        "LifecycleAction",
        "ScenarioAssertion",
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
            assertions=(),
        ),
    )
    allowed_public_callables = {
        LifecycleAction: set(),
        ScenarioObservation: set(),
        ScenarioRow: set(),
    }

    for record in records:
        assert _public_callable_names(record) == allowed_public_callables[type(record)]
