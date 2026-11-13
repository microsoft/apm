"""Prune and the expected-path selector share deps/reachability.py (#3668)."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "install-deployment-uninstall-reachability"


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        pytest.param(
            "src/apm_cli/commands/prune.py",
            "expected_lock_keys |= retained_lock_keys(",
            "expected_lock_keys |= private_retained_keys(",
            id="prune-bypasses-owner",
        ),
        pytest.param(
            "src/apm_cli/commands/_helpers.py",
            "            if dep.get_unique_key() in retained:",
            "            if dep.depth is not None and dep.depth > 1:",
            id="selector-rederives-depth",
        ),
    ],
)
def test_retained_lock_key_mutation_is_rejected(path: str, before: str, after: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        ROOT, (RULE,), source_overrides={path: source.replace(before, after)}
    )
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)
