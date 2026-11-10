"""Static regression traps for marketplace locked-evidence consumers."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.marketplace_locked_evidence import RULE_ID
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).parents[2]


def test_marketplace_locked_evidence_routes_through_owners() -> None:
    """The real source tree must satisfy every scoped canonical-owner route."""
    report = run_selected_rules(_ROOT, (RULE_ID,))
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("policy/ci_checks.py", "marketplace_ref_mismatch(dep_ref, locked_dep)", "None"),
        ("policy/ci_checks.py", "lock.get_dependency(", "lock.dependencies.get("),
        ("install/plan.py", "lockfile.get_dependency(", "lockfile.dependencies.get("),
        ("drift.py", "marketplace_ref_mismatch(dep_ref, locked_dep)", "None"),
        ("drift.py", "satisfies_range(version, spec)", "True"),
        ("marketplace/resolver.py", "build_tag_regex(", "re.compile("),
    ],
)
def test_marketplace_locked_evidence_guard_rejects_bypasses(
    path: str, before: str, after: str
) -> None:
    """In-memory mutations avoid whole-repository copies or source edits."""
    source_path = f"src/apm_cli/{path}"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        _ROOT, (RULE_ID,), source_overrides={source_path: source.replace(before, after)}
    )
    assert any(item.rule_id == RULE_ID for item in report.violations)
