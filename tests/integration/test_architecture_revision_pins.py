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
    resolver = (ROOT / "src/apm_cli/install/phases/resolve.py").read_text(encoding="utf-8")
    dependency_resolver = (ROOT / "src/apm_cli/deps/apm_resolver.py").read_text(encoding="utf-8")
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)

    assert owner.count("class RevisionPinResolutionResult:") == 1
    assert owner.count("class RevisionPinSkip:") == 1
    assert owner.count("def resolve_revision_pin_updates(") == 1
    assert '.removesuffix(".git")' in owner
    assert "max(candidates, key=lambda item: (item[0], item[1]))" in owner
    assert "logger.revision_pins_retained(resolution.skips)" in command
    assert "logger.revision_pin_resolution_failed(e)" in command
    assert "revision_pin_updates = revision_pin_resolution.updates" in command
    assert "root_package=ctx.apm_package" in resolver
    assert (
        "root_package = replace(root_package, source_path=project_root.resolve())"
        in dependency_resolver
    )
    assert "Revision-pin updates and retained SHAs share one typed outcome owner" in (
        rule.description
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "logger.revision_pins_retained(resolution.skips)",
            "logger.revision_pins_retained(())",
        ),
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


def test_revision_pin_guard_rejects_unstaged_root_resolution() -> None:
    """The install resolver must not reload pre-consent refs from disk."""
    path = "src/apm_cli/install/phases/resolve.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "resolver.resolve_dependencies(\n        manifest_anchor,\n"
        "        root_package=ctx.apm_package,\n    )",
        "resolver.resolve_dependencies(manifest_anchor)",
        1,
    )

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert _violated(report)


def test_revision_pin_guard_rejects_unanchored_staged_root() -> None:
    """A staged root must preserve portable local-dependency anchoring."""
    path = "src/apm_cli/deps/apm_resolver.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "root_package = replace(root_package, source_path=project_root.resolve())",
        "root_package = root_package",
        1,
    )

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert _violated(report)


def test_revision_pin_guard_rejects_nondeterministic_tag_tie() -> None:
    """Equal-precedence tags must not depend on remote record order."""
    path = "src/apm_cli/deps/revision_pins.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "max(candidates, key=lambda item: (item[0], item[1]))",
        "max(candidates, key=lambda item: item[0])",
        1,
    )

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={path: mutated},
    )

    assert _violated(report)
