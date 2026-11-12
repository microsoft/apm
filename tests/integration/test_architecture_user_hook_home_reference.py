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
            "src/apm_cli/integration/hook_integrator.py",
            'return getattr(target, "user_hook_home_reference", None)',
            "return None",
            id="rewriter-ignores-profile",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            'home_reference.reference_for(\n                    source_key or "command", '
            "quote=path_quote, exec_form=exec_form\n                )",
            '"$HOME"',
            id="rewriter-hard-codes-home",
        ),
        pytest.param(
            "src/apm_cli/integration/hook_integrator.py",
            "            if reference is not None:\n",
            '            if reference is not None and sys.platform != "win32":\n',
            id="rewriter-gates-on-host-os",
        ),
        pytest.param(
            "src/apm_cli/integration/kiro_hook_integrator.py",
            'integrator._home_reference_for_hook_rewrite(target, "kiro", user_scope)',
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
