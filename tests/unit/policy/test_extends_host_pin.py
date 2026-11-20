"""Security Finding F1: extends: host pinning + redirect refusal.

A malicious or compromised org policy author could otherwise set
``extends: "evil.example.com/org/.github"`` and route ``git credential
fill`` (and any subsequent Authorization header) at an attacker-
controlled host. These tests pin the ``extends:`` chain to the leaf
policy's origin host and verify HTTP redirects are refused.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _assert_extends_host_in_message(msg: str, expected_host: str) -> None:
    """Assert *expected_host* appears as the parsed ``extends host:`` token.

    Anchored on the production error format
    ``... extends host: <host>); ...`` so CodeQL's
    ``py/incomplete-url-substring-sanitization`` rule is satisfied --
    we do not bare-substring-match a hostname against an arbitrary
    string.
    """
    match = re.search(r"extends host:\s*([^\s)]+)", msg)
    assert match is not None, f"no 'extends host:' token in message: {msg!r}"
    assert match.group(1) == expected_host


def _assert_leaf_host_in_message(msg: str, expected_host: str) -> None:
    """Assert *expected_host* appears as the parsed ``leaf host:`` token."""
    match = re.search(r"leaf host:\s*([^\s,)]+)", msg)
    assert match is not None, f"no 'leaf host:' token in message: {msg!r}"
    assert match.group(1) == expected_host


from apm_cli.policy.discovery import (  # noqa: E402
    PolicyFetchResult,
    discover_policy_with_chain,
)
from apm_cli.policy.inheritance import PolicyInheritanceError  # noqa: E402
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy  # noqa: E402

_PATCH_DISCOVER = "apm_cli.policy.discovery.discover_policy"
_PATCH_WRITE_CACHE = "apm_cli.policy.discovery._write_cache"


def _make_policy(*, enforcement="warn", extends=None, deny=()):
    return ApmPolicy(
        enforcement=enforcement,
        extends=extends,
        dependencies=DependencyPolicy(deny=deny),
    )


def _make_fetch(policy=None, source="org:contoso/.github", outcome="found"):
    return PolicyFetchResult(policy=policy, source=source, outcome=outcome, cached=False)


# ----------------------------------------------------------------------
# Host-pin enforcement on extends: chain walk
# ----------------------------------------------------------------------


class TestExtendsHostPin:
    """extends: refs may only resolve against the leaf's origin host."""

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_cross_host_rejected_url_form(self, mock_discover, mock_write_cache):
        """Leaf at github.com cannot extend a full URL on evil.example.com."""
        leaf = _make_policy(enforcement="warn", extends="https://evil.example.com/policy.yml")
        leaf_fetch = _make_fetch(policy=leaf, source="org:contoso/.github")
        # Only the leaf fetch should run. Validation must happen BEFORE
        # the parent fetch so credentials are never sent to evil host.
        mock_discover.return_value = leaf_fetch

        with pytest.raises(PolicyInheritanceError) as exc_info:
            discover_policy_with_chain(Path("/fake"))

        msg = str(exc_info.value)
        assert "cross-host" in msg
        _assert_extends_host_in_message(msg, "evil.example.com")
        # Only one discover call (the leaf): parent must not have been
        # fetched -- credential leak prevented.
        assert mock_discover.call_count == 1
        mock_write_cache.assert_not_called()

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_cross_host_rejected_host_prefix_shorthand(
        self, mock_discover, mock_write_cache
    ):
        """Leaf at github.com cannot extend `evil.example.com/org/.github`."""
        leaf = _make_policy(enforcement="warn", extends="evil.example.com/org/.github")
        leaf_fetch = _make_fetch(policy=leaf, source="org:contoso/.github")
        mock_discover.return_value = leaf_fetch

        with pytest.raises(PolicyInheritanceError) as exc_info:
            discover_policy_with_chain(Path("/fake"))

        msg = str(exc_info.value)
        assert "cross-host" in msg
        _assert_extends_host_in_message(msg, "evil.example.com")
        _assert_leaf_host_in_message(msg, "github.com")
        assert mock_discover.call_count == 1

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_same_host_owner_repo_shorthand_allowed(self, mock_discover, mock_write_cache):
        """`owner/repo` shorthand is intrinsically same-host -> allowed."""
        leaf = _make_policy(enforcement="warn", extends="parent-org/.github")
        parent = _make_policy(enforcement="block")

        leaf_fetch = _make_fetch(policy=leaf, source="org:contoso/.github")
        parent_fetch = _make_fetch(policy=parent, source="org:parent-org/.github")
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        result = discover_policy_with_chain(Path("/fake"))
        # Chain walk completed -- enforcement tightened by parent.
        assert result.policy.enforcement == "block"

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_raw_githubusercontent_rejected(self, mock_discover, mock_write_cache):
        """Strict pin: raw.githubusercontent.com != github.com -> rejected.

        Decision: we pin strictly to the leaf's user-facing host. GitHub's
        internal use of raw.githubusercontent.com for content fetches is
        an implementation detail of the API path; user-facing
        ``extends:`` values must name the same host (github.com) as the
        leaf. This avoids a future bypass where a near-namespace host
        becomes attacker-controllable.
        """
        leaf = _make_policy(
            enforcement="warn",
            extends="https://raw.githubusercontent.com/org/repo/main/policy.yml",
        )
        leaf_fetch = _make_fetch(policy=leaf, source="org:contoso/.github")
        mock_discover.return_value = leaf_fetch

        with pytest.raises(PolicyInheritanceError) as exc_info:
            discover_policy_with_chain(Path("/fake"))
        _assert_extends_host_in_message(str(exc_info.value), "raw.githubusercontent.com")

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_ghes_same_host_allowed(self, mock_discover, mock_write_cache):
        """Leaf on ghes.contoso.com may extend within ghes.contoso.com."""
        leaf = _make_policy(
            enforcement="warn",
            extends="ghes.contoso.com/platform/.github",
        )
        parent = _make_policy(enforcement="block")
        leaf_fetch = _make_fetch(policy=leaf, source="org:ghes.contoso.com/contoso/.github")
        parent_fetch = _make_fetch(policy=parent, source="org:ghes.contoso.com/platform/.github")
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        result = discover_policy_with_chain(Path("/fake"))
        assert result.policy.enforcement == "block"

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_ghes_cross_host_rejected(self, mock_discover, mock_write_cache):
        """Leaf on ghes.contoso.com cannot extend onto github.com."""
        leaf = _make_policy(enforcement="warn", extends="github.com/org/.github")
        leaf_fetch = _make_fetch(policy=leaf, source="org:ghes.contoso.com/contoso/.github")
        mock_discover.return_value = leaf_fetch

        with pytest.raises(PolicyInheritanceError) as exc_info:
            discover_policy_with_chain(Path("/fake"))
        msg = str(exc_info.value)
        assert "cross-host" in msg
        _assert_leaf_host_in_message(msg, "ghes.contoso.com")
        _assert_extends_host_in_message(msg, "github.com")

    @patch(_PATCH_WRITE_CACHE)
    @patch(_PATCH_DISCOVER)
    def test_extends_org_shorthand_allowed(self, mock_discover, mock_write_cache):
        """`org` (no slash) shorthand is intrinsically same-host -> allowed."""
        leaf = _make_policy(enforcement="warn", extends="contoso")
        # The shorthand "contoso" -> the parent fetch will route via the
        # repo branch of discover_policy. We just need to verify validation
        # passes (no raise) and the parent fetch is attempted.
        parent = _make_policy(enforcement="block")
        leaf_fetch = _make_fetch(policy=leaf, source="org:contoso/.github")
        parent_fetch = _make_fetch(policy=parent, source="org:contoso/.github")
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        # Validation must pass (no cross-host error). Chain completes.
        result = discover_policy_with_chain(Path("/fake"))
        assert result.policy.enforcement == "block"


# ----------------------------------------------------------------------
# Redirect refusal in _fetch_from_url
# ----------------------------------------------------------------------
