"""Protect the marketplace ADO adapter's three existing owner handoffs."""

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-host-credential-resolution"
CLIENT = "src/apm_cli/marketplace/client.py"


def test_marketplace_ado_auth_owner_is_compliant() -> None:
    """The actual adapter retains fallback, rewrite safety and typed recovery."""
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "checkout_dir = auth_resolver.try_with_fallback(\n"
            "                host_info.host,\n"
            "                _checkout,\n"
            "                **fallback_kwargs,\n"
            "            )",
            "checkout_dir = _checkout(None, ado_base_env)",
        ),
        ("validate_git_url_rewrite_safety(source.url, ado_base_env)", "pass"),
        ("except AdoAuthChainExhaustedError as exc:", "except KeyError as exc:"),
    ],
    ids=["resolver-fallback", "rewrite-safety", "typed-chain-recovery"],
)
def test_marketplace_ado_auth_guard_rejects_removed_handoff(old: str, new: str) -> None:
    """Valid Python bypasses must fail even with old names in comments/imports."""
    source = (ROOT / CLIENT).read_text(encoding="utf-8")
    assert source.count(old) == 1
    mutated = (
        source.replace(old, new, 1)
        + "\n"
        + "\n".join(f"# Former handoff: {line}" for line in old.splitlines())
    )
    ast.parse(mutated)
    report = run_selected_rules(ROOT, (RULE,), source_overrides={CLIENT: mutated})
    assert report.failures == ()
    assert {finding.rule_id for finding in report.violations} == {RULE}
