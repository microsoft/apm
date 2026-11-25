"""The four v0.39 upgrade failure messages name the exact fix (#5471)."""

from __future__ import annotations

import pytest

from apm_cli.deps.git_remote_ops import exact_branch_or_tag_sha
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.policy_checks import mcp_policy_checks
from apm_cli.policy.schema import ApmPolicy, McpPolicy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-outcome")]


def test_short_sha_refusal_asks_for_the_full_sha_not_a_sha() -> None:
    records = [("a" * 40, "refs/heads/main")]
    with pytest.raises(RuntimeError) as refusal:
        exact_branch_or_tag_sha(records, "abc1234", "example.com/o/r")
    text = str(refusal.value)
    assert "full 40-character SHA" in text
    assert "or a branch or tag" in text
    assert "use a commit SHA" not in text.lower()


def test_trust_transitive_audit_summary_names_the_alternative() -> None:
    server = MCPDependency(name="srv", registry=False, transport="http", url="https://x.invalid/s")
    server.resolved_by = "leaf"
    server.declared_depth = 2
    policy = ApmPolicy(enforcement="warn", mcp=McpPolicy(trust_transitive=False))
    check = {c.name: c for c in mcp_policy_checks([server], policy)}["mcp-trust-transitive"]
    assert not check.passed
    assert "add them to mcp.allow or set mcp.trust_transitive: true" in check.message
