"""Dual guard for shared MCP authentication ownership and provenance."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-host-credential-resolution"
ADAPTER = "src/apm_cli/adapters/client/base.py"


def test_mcp_auth_owner_rule_passes() -> None:
    """The shared adapter retains the registered auth route and provenance."""
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    "old,new",
    [
        ("resolver.resolve_github_mcp_token(", "token_manager_class().get_token_for_purpose("),
        ("source_only=self._supports_runtime_env_substitution", "source_only=False"),
        ('isinstance(header.get("value"), ManifestHeaderValue)', "True"),
    ],
)
def test_mcp_auth_guard_rejects_owner_or_provenance_bypass(old: str, new: str) -> None:
    """Removing the credential route, source-only mode or provenance trips the guard."""
    source = (ROOT / ADAPTER).read_text(encoding="utf-8")
    assert source.count(old) == 1
    report = run_selected_rules(ROOT, (RULE,), source_overrides={ADAPTER: source.replace(old, new)})
    assert report.failures == ()
    assert {item.rule_id for item in report.violations} == {RULE}
