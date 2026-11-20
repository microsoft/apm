"""Owner tests for dependency ``skills:`` / ``targets:`` validation.

User-facing error text for the subset fields is a promise: every message is
pinned exactly (dependency-selection-merge, see tests/mutation/floors.json).
"""

from __future__ import annotations

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.dependency.subsets import parse_skill_subset, parse_target_subset

pytestmark = pytest.mark.unit

_VALID = ", ".join(sorted(KNOWN_TARGETS))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("x", "'skills' field must be a list of skill names"),
        (
            [],
            "skills: must contain at least one name; "
            "remove the field to install all skills in the bundle.",
        ),
        ([" "], "Each entry in 'skills' must be a non-empty string"),
        (["../x"], "Invalid skills/<name> '../x': segment '..' is a traversal sequence"),
    ],
)
def test_skill_subset_errors_are_exact(raw: object, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_skill_subset(raw)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("x", "'targets' field must be a list of target names"),
        (
            [],
            "targets: must contain at least one target; "
            "remove the field to route primitives to all active install targets.",
        ),
        ([3], "Each entry in 'targets' must be a non-empty string"),
        (["clade"], f"Unknown target 'clade'. Valid targets: {_VALID}. Did you mean 'claude'?"),
        (["zzzzzz"], f"Unknown target 'zzzzzz'. Valid targets: {_VALID}."),
    ],
)
def test_target_subset_errors_are_exact(raw: object, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_target_subset(raw)
    assert str(caught.value) == message


def test_target_aliases_record_their_canonical_target() -> None:
    assert parse_target_subset(["vscode", " Claude ", "copilot"]) == ["claude", "copilot"]
