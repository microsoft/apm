"""Unit tests for the shared chain-aware discovery seam.

Covers:
- ``discover_policy_with_chain`` returns same chain_refs as gate-phase path
- ``no_policy=True`` short-circuits to outcome="disabled"
- ``APM_POLICY_DISABLE=1`` short-circuits to outcome="disabled"
- Cache hit path returns merged effective policy + chain_refs
- Cache miss path calls resolve_policy_chain and writes cache atomically

These tests validate that ALL command sites (gate-phase, --mcp, --dry-run)
share one discovery+chain implementation via
``apm_cli.policy.discovery.discover_policy_with_chain``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from apm_cli.policy.discovery import (
    PolicyFetchResult,
    discover_policy_with_chain,
)
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

# Patch targets -- all live in apm_cli.policy.discovery (same module)
_PATCH_DISCOVER = "apm_cli.policy.discovery.discover_policy"
_PATCH_WRITE_CACHE = "apm_cli.policy.discovery._write_cache"


# -- Helpers ---------------------------------------------------------------


def _make_policy(*, enforcement="warn", extends=None, deny=()):
    """Build a minimal ApmPolicy for testing."""
    return ApmPolicy(
        enforcement=enforcement,
        extends=extends,
        dependencies=DependencyPolicy(deny=deny),
    )


def _make_fetch(
    policy=None,
    outcome="found",
    source="org:contoso/.github",
    cached=False,
    error=None,
    cache_age_seconds=None,
):
    """Build a PolicyFetchResult for testing."""
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=cached,
        outcome=outcome,
        error=error,
        cache_age_seconds=cache_age_seconds,
    )


# ======================================================================
# Escape hatches
# ======================================================================


class TestEscapeHatches:
    """no_policy and APM_POLICY_DISABLE short-circuit to disabled."""

    def test_no_policy_true_returns_disabled(self):
        result = discover_policy_with_chain(Path("/fake"), no_policy=True)
        assert result.outcome == "disabled"
        assert result.policy is None

    def test_env_var_disable_returns_disabled(self):
        with patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"}):
            result = discover_policy_with_chain(Path("/fake"))
        assert result.outcome == "disabled"
        assert result.policy is None

    def test_env_var_not_set_proceeds(self):
        """Without the env var, discovery actually runs."""
        policy = _make_policy()
        fetch = _make_fetch(policy=policy)

        with patch(_PATCH_DISCOVER, return_value=fetch):
            result = discover_policy_with_chain(Path("/fake"))
        assert result.outcome == "found"
        assert result.policy is not None


# ======================================================================
# Chain resolution
# ======================================================================


class TestChainResolution:
    """discover_policy_with_chain resolves extends: chains."""

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_triggers_chain_resolution(
        self, mock_discover, mock_write_cache
    ):
        """A leaf with extends: triggers parent fetch + merge + cache write."""
        leaf = _make_policy(enforcement="warn", extends="parent-org/.github")
        leaf_fetch = _make_fetch(
            policy=leaf, source="org:contoso/.github", cached=False
        )

        parent = _make_policy(
            enforcement="block", deny=("evil/*",)
        )
        parent_fetch = _make_fetch(
            policy=parent, source="org:parent-org/.github"
        )

        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        result = discover_policy_with_chain(Path("/fake"))

        # The merged policy should tighten to block (parent's enforcement)
        assert result.policy.enforcement == "block"
        # Parent's deny list should be merged in
        assert "evil/*" in result.policy.dependencies.deny

        # Cache writer should have been called with real chain_refs
        assert mock_write_cache.called
        kw = mock_write_cache.call_args
        chain_refs = kw.kwargs.get("chain_refs") or kw[1].get("chain_refs")
        assert chain_refs is not None
        assert len(chain_refs) == 2
        assert "parent-org/.github" in chain_refs[0]
        assert "contoso/.github" in chain_refs[1]

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_no_extends_no_chain_resolution(
        self, mock_discover, mock_write_cache
    ):
        """Without extends:, no chain resolution or re-caching happens."""
        policy = _make_policy(enforcement="warn")
        fetch = _make_fetch(policy=policy, cached=False)
        mock_discover.return_value = fetch

        result = discover_policy_with_chain(Path("/fake"))
        mock_write_cache.assert_not_called()
        assert result.policy.enforcement == "warn"

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_cached_result_skips_chain_resolution(
        self, mock_discover, mock_write_cache
    ):
        """When result is from cache, skip re-resolution even with extends:."""
        policy = _make_policy(enforcement="warn", extends="org")
        fetch = _make_fetch(policy=policy, cached=True)
        mock_discover.return_value = fetch

        result = discover_policy_with_chain(Path("/fake"))
        mock_write_cache.assert_not_called()
        # discover_policy called only once (no parent fetch)
        assert mock_discover.call_count == 1


# ======================================================================
# Cache paths
# ======================================================================


class TestCachePaths:
    """Cache hit and cache miss paths."""

    @patch(_PATCH_DISCOVER)
    def test_cache_hit_returns_merged_policy(self, mock_discover):
        """Cached result (no extends) returns immediately."""
        policy = _make_policy(enforcement="block", deny=("bad/*",))
        fetch = _make_fetch(
            policy=policy, cached=True, cache_age_seconds=300
        )
        mock_discover.return_value = fetch

        result = discover_policy_with_chain(Path("/fake"))
        assert result.policy.enforcement == "block"
        assert result.cached is True
        assert result.cache_age_seconds == 300

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_cache_miss_fetches_and_writes(
        self, mock_discover, mock_write_cache
    ):
        """Fresh fetch with extends: merges and writes cache atomically."""
        leaf = _make_policy(enforcement="warn", extends="hub/.github")
        leaf_fetch = _make_fetch(
            policy=leaf, source="org:team/.github", cached=False
        )
        parent = _make_policy(enforcement="block")
        parent_fetch = _make_fetch(
            policy=parent, source="org:hub/.github"
        )
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        result = discover_policy_with_chain(Path("/fake"))

        # Cache writer called with merged policy
        assert mock_write_cache.called
        written_policy = mock_write_cache.call_args[0][1]
        assert written_policy.enforcement == "block"


# ======================================================================
# Shared seam: gate-phase delegates here
# ======================================================================


class TestGatePhaseDelegate:
    """policy_gate._discover_with_chain delegates to the shared function."""

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_gate_discover_returns_same_as_shared(
        self, mock_discover, mock_write_cache
    ):
        """Gate-phase _discover_with_chain produces identical results."""
        from dataclasses import dataclass, field
        from typing import Any, List

        @dataclass
        class _FakeCtx:
            project_root: Path = field(default_factory=lambda: Path("/fake"))
            logger: Any = None
            no_policy: bool = False

        leaf = _make_policy(enforcement="warn", extends="parent/.github")
        leaf_fetch = _make_fetch(
            policy=leaf, source="org:child/.github", cached=False
        )
        parent = _make_policy(enforcement="block")
        parent_fetch = _make_fetch(
            policy=parent, source="org:parent/.github"
        )
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        from apm_cli.install.phases.policy_gate import _discover_with_chain

        ctx = _FakeCtx()
        result = _discover_with_chain(ctx)

        # Result should have merged enforcement
        assert result.policy.enforcement == "block"

        # chain_refs in cache should cover both
        kw = mock_write_cache.call_args
        chain_refs = kw.kwargs.get("chain_refs") or kw[1].get("chain_refs")
        assert len(chain_refs) == 2


# ======================================================================
# Preflight also uses shared seam
# ======================================================================


class TestPreflightUsesSharedSeam:
    """install_preflight.run_policy_preflight uses discover_policy_with_chain."""

    @patch(
        "apm_cli.policy.install_preflight.discover_policy_with_chain",
    )
    def test_preflight_calls_chain_aware_discovery(self, mock_chain_discover):
        """run_policy_preflight invokes the chain-aware shared function."""
        policy = _make_policy(enforcement="warn")
        fetch = _make_fetch(policy=policy)
        mock_chain_discover.return_value = fetch

        from apm_cli.policy.install_preflight import run_policy_preflight

        logger = MagicMock()
        run_policy_preflight(
            project_root=Path("/fake"),
            apm_deps=[],
            no_policy=False,
            logger=logger,
        )

        mock_chain_discover.assert_called_once_with(Path("/fake"))
