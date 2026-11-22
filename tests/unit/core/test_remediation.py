"""remediation-advice owner: the one shape of every remediation line (#4121)."""

from __future__ import annotations

import pytest

from apm_cli.core.remediation import (
    Remediation,
    either,
    either_sentence,
    kept_file_message,
    kept_note,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]

INSTALL = Remediation("install", "apm install", "restore the files")
UPDATE = Remediation("update", "apm update", "move to the new version")
PRUNE = Remediation("prune", "apm prune", "drop orphans")
STAGED = Remediation(
    "staged", "apm install", "redeploy", before="move it aside", then="commit the lockfile"
)


def test_either_with_one_remediation_is_its_clause() -> None:
    assert either(INSTALL) == "run 'apm install' to restore the files"


def test_either_joins_alternatives_as_options() -> None:
    assert either(INSTALL, UPDATE, PRUNE) == (
        "run 'apm install' to restore the files, or 'apm update' to move to the new version, "
        "or 'apm prune' to drop orphans"
    )


def test_either_keeps_the_first_remediation_steps() -> None:
    assert either(STAGED, UPDATE) == (
        "move it aside, then run 'apm install' to redeploy, then commit the lockfile, "
        "or 'apm update' to move to the new version"
    )


def test_either_sentence_capitalizes_and_ends_with_a_period() -> None:
    assert either_sentence(INSTALL, UPDATE) == (
        "Run 'apm install' to restore the files, or 'apm update' to move to the new version."
    )
    assert either_sentence(STAGED) == (
        "Move it aside, then run 'apm install' to redeploy, then commit the lockfile."
    )


def test_kept_note_without_a_next_step() -> None:
    assert kept_note("a.md", "it was edited") == "Kept a.md: it was edited."


def test_kept_note_with_a_next_step() -> None:
    assert kept_note("a.md", "it was edited", "Delete it manually.") == (
        "Kept a.md: it was edited. Delete it manually."
    )


def test_kept_note_with_an_empty_next_step_adds_nothing() -> None:
    assert kept_note("a.md", "it was edited", "") == "Kept a.md: it was edited."


def test_kept_file_message_appends_the_remedy_sentence() -> None:
    assert kept_file_message("a.md", "it was edited", INSTALL, PRUNE) == (
        "Kept a.md: it was edited. "
        "Run 'apm install' to restore the files, or 'apm prune' to drop orphans."
    )


def test_sentence_upper_cases_only_the_first_character() -> None:
    lower = Remediation("x", "apm install", "fix it", before="first back it UP")
    assert either_sentence(lower) == "First back it UP, then run 'apm install' to fix it."
