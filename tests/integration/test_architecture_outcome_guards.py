"""Integration guardrails for install and policy outcome authorities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_install_result_disposition_owns_cli_exit_code() -> None:
    """Service classification and adapter exit translation must agree."""
    from apm_cli.install.outcome import finalize_install_result
    from apm_cli.models.results import InstallDisposition, InstallResult

    diagnostics = MagicMock(error_count=1, has_critical_security=False)
    result = finalize_install_result(
        InstallResult(diagnostics=diagnostics),
        force=False,
    )

    assert result.disposition is InstallDisposition.FAILED
    assert result.exit_code == 1


def test_manifest_inheritance_cannot_relax_explicit_includes() -> None:
    """Either ancestor or child may tighten explicit-include enforcement."""
    from apm_cli.policy.inheritance import merge_policies
    from apm_cli.policy.schema import ApmPolicy, ManifestPolicy

    parent_true = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))
    child_false = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=False))
    parent_false = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=False))
    child_true = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))

    assert merge_policies(parent_true, child_false).manifest.require_explicit_includes
    assert merge_policies(parent_false, child_true).manifest.require_explicit_includes


def test_explicit_policy_uses_chain_aware_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit leaves must resolve ancestors through the shared entry point."""
    from apm_cli.policy import discovery
    from apm_cli.policy.schema import ApmPolicy

    calls: list[str | None] = []
    leaf = discovery.PolicyFetchResult(
        policy=ApmPolicy(extends="owner/parent"),
        source="org:owner/leaf",
        outcome="found",
    )
    missing_parent = discovery.PolicyFetchResult(
        policy=None,
        source="org:owner/parent",
        outcome="cache_miss_fetch_fail",
        error="unreachable",
    )

    def fake_discover(_root, *, policy_override=None, **_kwargs):
        calls.append(policy_override)
        return leaf if len(calls) == 1 else missing_parent

    monkeypatch.setattr(discovery, "discover_policy", fake_discover)

    result = discovery.discover_policy_with_chain(
        tmp_path,
        policy_override="owner/leaf",
        no_cache=True,
    )

    assert calls == ["owner/leaf", "owner/parent"]
    assert result.outcome == "incomplete_chain"
    assert result.policy is None


def test_incomplete_policy_chain_always_fails_closed() -> None:
    """A partial ancestor set must never become an enforceable policy."""
    from apm_cli.install.errors import PolicyViolationError
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.outcome_routing import route_discovery_outcome

    result = PolicyFetchResult(
        policy=None,
        source="org:owner/leaf",
        outcome="incomplete_chain",
        error="parent unreachable",
    )

    with pytest.raises(PolicyViolationError):
        route_discovery_outcome(
            result,
            logger=MagicMock(),
            fetch_failure_default="warn",
        )
