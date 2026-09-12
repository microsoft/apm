"""Root native skill copies cannot bypass the canonical collision guard."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
PATH = "src/apm_cli/integration/skill_integrator.py"
RULE = "install-deployment-base-integrator"


def test_native_skill_collision_guard_is_registered_and_clean() -> None:
    result = run_selected_rules(ROOT, (RULE,))
    assert not result.failures
    assert not result.violations


def test_native_skill_collision_guard_rejects_bypass() -> None:
    source = (ROOT / PATH).read_text()
    mutated = source.replace("if self.check_collision(", "if self.parallel_collision_policy(")
    assert mutated != source
    result = run_selected_rules(ROOT, (RULE,), source_overrides={PATH: mutated})
    assert any(
        item.path == PATH and "BaseIntegrator.check_collision" in item.message
        for item in result.violations
    )
