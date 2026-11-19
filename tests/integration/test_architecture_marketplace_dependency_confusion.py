"""The #1326 admission guard rejects install paths that bypass its owner (#3913)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.marketplace_dependency_confusion import RULE_ID
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).resolve().parents[2]


def test_dependency_confusion_admission_routes_through_owner() -> None:
    report = run_selected_rules(_ROOT, (RULE_ID,))
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "deps/apm_resolver.py",
            "        admit_marketplace_resolution(resolution)\n",
            "",
        ),
        (
            "commands/install.py",
            "                    admit_marketplace_resolution(resolution)\n",
            "                    if resolution.cross_repo_misconfig_risk:\n"
            "                        continue\n",
        ),
        (
            "commands/outdated.py",
            "def _check_marketplace_ref(dep, verbose):\n",
            "def _check_marketplace_ref(dep, verbose):\n"
            "    resolve_marketplace_plugin(dep.marketplace_plugin_name, dep.discovered_via)\n",
        ),
    ],
)
def test_dependency_confusion_guard_rejects_bypasses(path: str, before: str, after: str) -> None:
    """Skipping the owner, re-reading the sentinel, or a new unadmitted resolver call fails."""
    source_path = f"src/apm_cli/{path}"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    assert source.count(before) == 1
    report = run_selected_rules(
        _ROOT, (RULE_ID,), source_overrides={source_path: source.replace(before, after)}
    )
    assert any(item.rule_id == RULE_ID for item in report.violations)
