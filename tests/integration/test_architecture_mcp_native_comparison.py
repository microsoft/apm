"""Same-name MCP comparison cannot fall back to a per-runtime name gate (#3681)."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).parents[2]
RULE = "install-deployment-mcp-native-comparison"
FLOW = "src/apm_cli/integration/mcp_integrator_install.py"
OWNER = "src/apm_cli/install/mcp/ownership.py"


def test_mcp_native_comparison_guard_is_registered_and_clean() -> None:
    result = run_selected_rules(ROOT, (RULE,))
    assert not result.failures
    assert not result.violations


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (FLOW, "if rt in protected_clients:", 'if rt in {"claude", "codex"}:'),
        (
            FLOW,
            "    protected_clients = native_comparison_clients(\n",
            "    protected_clients = _local_runtime_gate(\n",
        ),
        (OWNER, "if client.compares_native_connections:", "if runtime == 'claude':"),
    ],
)
def test_mcp_native_comparison_guard_rejects_bypass(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text()
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(ROOT, (RULE,), source_overrides={path: mutated})
    assert any(item.path == path and item.rule_id == RULE for item in result.violations)
