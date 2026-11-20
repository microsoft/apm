"""Read-only preflight shares deployed source selection and exact-path ownership (pure logic cases)."""

from pathlib import Path

import pytest

from apm_cli.integration.skill_preflight import (
    SkillConflict,
    format_skill_conflicts,
)

pytestmark = pytest.mark.unit


def test_multiple_conflicts_keep_each_source_and_one_nondestructive_remedy() -> None:
    conflicts = [
        SkillConflict("review", "first", Path("first/SKILL.md"), ".claude/review", ("claude",)),
        SkillConflict("lint", "second", Path("second/SKILL.md"), ".agents/lint", ("codex",)),
    ]
    message = format_skill_conflicts(conflicts)
    for conflict in conflicts:
        assert conflict.message() in message
    assert message.count("Rename the local skill") == 1
    assert "skills: or targets: selection in apm.yml" in message
    assert "--force" not in message
    assert message.isascii()
