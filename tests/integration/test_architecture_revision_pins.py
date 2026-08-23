"""Architecture guardrails for revision-pin resolution outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.models import RunReport
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-revision-pin-outcome"


def _violated(report: RunReport) -> bool:
    """Return whether the revision-pin owner rule reported a violation."""
    return any(item.rule_id == RULE_ID for item in report.violations)


def _command_source() -> str:
    """Return the update command source used by mutation tests."""
    return (ROOT / "src/apm_cli/commands/update.py").read_text(encoding="utf-8")


def test_revision_pin_resolution_has_single_owner() -> None:
    """The registered rule must defend the typed owner and both consumers."""
    owner = (ROOT / "src/apm_cli/deps/revision_pins.py").read_text(encoding="utf-8")
    command = _command_source()
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert owner.count("class RevisionPinResolutionResult:") == 1
    assert owner.count("class RevisionPinSkip:") == 1
    assert owner.count("def resolve_revision_pin_updates(") == 1
    assert "for skipped in resolution.skips:" in command
    assert "revision_pin_updates = revision_pin_resolution.updates" in command
    assert "Revision-pin updates and retained SHAs share one typed outcome owner" in (
        rule.description
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("for skipped in resolution.skips:", "for skipped in ():"),
        (
            "revision_pin_updates = revision_pin_resolution.updates",
            "revision_pin_updates = ()",
        ),
    ],
)
def test_revision_pin_guard_rejects_discarded_outcomes(old: str, new: str) -> None:
    """The boundary rule must reject dropping either typed outcome collection."""
    command = _command_source()
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={"src/apm_cli/commands/update.py": command.replace(old, new, 1)},
    )

    assert _violated(report)


def test_revision_pin_guard_rejects_command_local_tag_lookup() -> None:
    """The command cannot restore an independent annotated-tag decision."""
    command = _command_source() + '\nfind_latest_annotated_tag("origin")\n'
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={"src/apm_cli/commands/update.py": command},
    )

    assert _violated(report)
