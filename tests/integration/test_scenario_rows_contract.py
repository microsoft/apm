from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.utils.scenario_rows import (
    LifecycleAction,
    ScenarioObservation,
    ScenarioRow,
)


def _named_assertion(observation: object) -> None:
    assert observation is not None


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


def test_row_exposes_no_execution_or_discovery_surface(tmp_path: Path) -> None:
    row = ScenarioRow(
        id="plain",
        source_inputs=(tmp_path,),
        lifecycle_actions=(),
        assertions=(),
    )

    forbidden = {
        "execute",
        "run",
        "discover",
        "register",
        "hooks",
        "plugins",
    }
    assert forbidden.isdisjoint(dir(row))
