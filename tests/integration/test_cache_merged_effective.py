"""Org-policy discovery reads the project git remote and serves a warm cache.

Spawns real ``git`` to create the project remote, so it lives in
tests/integration; the cache-layer contract tests stay in
tests/unit/policy/test_cache_merged_effective.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.policy.discovery import _read_cache_entry, discover_policy_with_chain
from tests.utils.simulated_ado_chain import StaticPolicyResolver

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-policy-remote-origin-reader", "P-OWN-cached-policy-shape"),
]


def test_ghes_org_sentinel_uses_project_remote_host_and_warm_cache(
    tmp_path: Path,
) -> None:
    leaf_ref = "ghes.contoso.com/team/policy"
    leaf_yaml = """
name: team-policy
extends: org
dependencies:
  deny: [team/blocked]
"""
    parent_yaml = """
name: platform-policy
enforcement: block
dependencies:
  require_pinned_constraint: true
"""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://ghes.contoso.com/platform/application.git",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    observed_urls: list[str] = []
    expected_paths = [
        "/api/v3/repos/team/policy/contents/apm-policy.yml",
        "/api/v3/repos/platform/.github-private/contents/apm-policy.yml",
        "/api/v3/repos/platform/.github/contents/apm-policy.yml",
    ]

    def get(url: str, **_kwargs) -> MagicMock:
        parsed = urlparse(url)
        assert parsed.hostname == "ghes.contoso.com"
        assert parsed.path == expected_paths[len(observed_urls)]
        observed_urls.append(url)
        response = MagicMock()
        response.headers = {}
        if parsed.path == expected_paths[1]:
            response.status_code = 404
        else:
            response.status_code = 200
            response.json.return_value = {
                "content": leaf_yaml if parsed.path == expected_paths[0] else parent_yaml
            }
        return response

    with (
        patch(
            "apm_cli.policy.discovery._policy_auth_resolver",
            return_value=StaticPolicyResolver(None),
        ),
        patch("apm_cli.policy.discovery.policy_get", side_effect=get) as transport,
    ):
        cold = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)
        assert cold.policy is not None
        assert cold.policy.enforcement == "block"
        assert cold.policy.dependencies.deny == ("team/blocked",)
        assert cold.policy.dependencies.require_pinned_constraint is True
        assert transport.call_count == 3

        transport.side_effect = AssertionError("network fetch attempted on warm cache hit")
        warm = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)

    assert [(urlparse(url).hostname, urlparse(url).path) for url in observed_urls] == [
        ("ghes.contoso.com", path) for path in expected_paths
    ]
    assert warm.cached is True
    assert warm.policy == cold.policy
    cache_entry = _read_cache_entry(leaf_ref, tmp_path)
    assert cache_entry is not None
    assert cache_entry.chain_refs == [
        "ghes.contoso.com/platform/.github",
        leaf_ref,
    ]
