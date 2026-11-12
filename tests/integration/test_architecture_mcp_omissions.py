"""Static regression traps for the sole MCP omission decision owner."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
_ROOT = Path(__file__).resolve().parents[2]
_RULE = "mutation_writes.mcp_omission_evidence"


def test_mcp_omission_owner_routes_are_intact() -> None:
    report = run_selected_rules(_ROOT, {_RULE})
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    "path,old,new",
    [
        (
            "src/apm_cli/policy/ci_checks.py",
            ").with_recorded_omissions(lock)",
            ")",
        ),
        (
            "src/apm_cli/install/mcp/integration.py",
            "include_dependency_mcp=should_install",
            "include_dependency_mcp=True",
        ),
        (
            "src/apm_cli/install/mcp/integration.py",
            "current_view if should_install else None",
            "current_view",
        ),
        (
            "src/apm_cli/integration/mcp_config_view.py",
            "if valid and name not in admitted:",
            "if valid:",
        ),
    ],
)
def test_mcp_omission_owner_bypass_is_rejected(path: str, old: str, new: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(
        _ROOT, {_RULE}, source_overrides={path: source.replace(old, new, 1)}
    )
    assert report.failures == ()
    assert any(item.rule_id == _RULE and item.path == path for item in report.violations)


def test_duplicate_mcp_omission_authority_is_rejected() -> None:
    path = "src/apm_cli/policy/ci_checks.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    report = run_selected_rules(
        _ROOT,
        {_RULE},
        source_overrides={
            path: source + "\ndef with_recorded_omissions(lock):\n    return lock.mcp_omissions\n"
        },
    )
    assert report.failures == ()
    assert any(item.rule_id == _RULE and item.path == path for item in report.violations)
