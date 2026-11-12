"""Static mutation proofs for plugin hook producer owner routing."""

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "mutation_writes.hook_command_vocabulary"


@pytest.mark.parametrize(
    "owner_call",
    [
        "iter_plugin_root_paths(",
        "resolve_plugin_root_hook_script(",
        "walk_hook_commands(",
        "iter_deployable_hook_bundle_files(",
    ],
)
def test_plugin_hook_projection_cannot_bypass_shared_owner(owner_call: str) -> None:
    path = "src/apm_cli/bundle/plugin_hooks.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(owner_call) == 1
    mutated = source.replace(owner_call, "competing_owner(", 1)
    ast.parse(mutated)
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == RULE for item in report.violations)


def test_second_plugin_root_resource_resolver_is_rejected() -> None:
    path = "src/apm_cli/bundle/plugin_hooks.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = (
        source + "\n\ndef resolve_plugin_root_hook_script(root, path):\n    return root / path\n"
    )
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any("belongs to hook_source_selection.py" in item.message for item in report.violations)
