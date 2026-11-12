"""Static regression guards for the user-scope hook home reference owner (#2821)."""

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "mutation_writes.user_hook_home_reference"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        pytest.param(
            "src/apm_cli/integration/targets.py",
            "user_hook_home_reference: UserHookHomeReference | None = None",
            "user_hook_home_reference: object = None",
            id="owner-field",
        ),
        pytest.param(
            "src/apm_cli/integration/targets.py",
            "def reference_for(",
            "def reference_or_none(",
            id="owner-decision",
        ),
        pytest.param(
            "src/apm_cli/integration/targets.py",
            "def for_handler(",
            "def handler_reference(",
            id="owner-exec-form",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            'reference = getattr(target, "user_hook_home_reference", None) if user_scope else None',
            "reference = None",
            id="rewriter-ignores-profile",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            "return home_reference.anchor(path, source_key, quote=path_quote)",
            'return f"$HOME/{target_rel}"',
            id="rewriter-hard-codes-home",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            "            if home_reference is None:\n",
            '            if home_reference is None or sys.platform == "win32":\n',
            id="rewriter-gates-on-host-os",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            "home_reference and home_reference.for_handler(hook)",
            "home_reference",
            id="rewriter-ignores-exec-form",
        ),
        pytest.param(
            "src/apm_cli/integration/kiro_hook_integrator.py",
            "integrator._home_reference_for_hook_rewrite(target, user_scope)",
            "None",
            id="kiro-bypasses-owner",
        ),
    ],
)
def test_user_hook_home_reference_cannot_bypass_owner(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = source.replace(old, new, 1)
    ast.parse(mutated, filename=path)
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)


def test_user_hook_home_reference_rule_is_clean_on_head() -> None:
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()
