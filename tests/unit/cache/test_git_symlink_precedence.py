"""Truth-table guard for the extracted configuration-selection predicate."""

from __future__ import annotations

import pytest

from apm_cli.utils.git_env import GitConfigEntry, _symlink_entry_wins

pytestmark = [pytest.mark.unit, pytest.mark.windows_compat]


@pytest.mark.parametrize(
    ("current_scope", "candidate_scope", "expected"),
    [
        (None, "local", True),
        ("global", "local", True),
        ("local", "command", True),
        ("command", "command", True),
        ("command", "local", False),
        ("system", "global", True),
    ],
)
def test_symlink_entry_wins_truth_table(
    current_scope: str | None, candidate_scope: str, expected: bool
) -> None:
    current = (
        GitConfigEntry(scope=current_scope, key="core.symlinks", value="true")
        if current_scope is not None
        else None
    )
    candidate = GitConfigEntry(scope=candidate_scope, key="core.symlinks", value="false")
    assert _symlink_entry_wins(current, candidate) is expected
