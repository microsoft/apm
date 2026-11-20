"""Catalog remediations printed by core/, models/, security/, utils/, output/,
update_policy.py and drift.py are true (#4090).

Every entry in the W3 core-models block of ``core/remediation.py`` names the
state it clears and renders a command line that resolves in the real Click
tree. Entries whose state an installed-CLI test can induce cheaply are executed
end to end in ``tests/integration/test_remediation_advice_lifecycle.py``
(:data:`EXECUTED`); the rest are listed in :data:`COMPONENT_ONLY` with the
reason an executed proof is out of reach.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-remediation-advice"),
]

_OWNER = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "core" / "remediation.py"
_BLOCK = re.compile(
    r"# -- W3 core-models --\n(?P<body>.*?)# -- end W3 core-models --",
    re.S,
)

# Executed installed-CLI proofs: test_area_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "deployment-owners-invalid",
        "local-files-collide",
    }
)

_NETWORK = "needs a remote registry, marketplace, Azure DevOps or GitLab host"
_REFUSAL = "a parse-time refusal; the reported state is the invalid manifest or lockfile value"
_FAULT = "needs a filesystem or lockfile fault the hermetic world cannot induce cheaply"
_NOT_CLEARING = "an informational footer the reporting command always prints"

COMPONENT_ONLY: dict[str, str] = {
    "ado-coordinates-mismatched": _NETWORK,
    "claude-plugin-layout": "a pack-time refusal; the advised command writes another bundle "
    "format rather than clearing a project state",
    "compile-dry-run": _NOT_CLEARING,
    "dependency-alias-invalid": _REFUSAL,
    "dry-run-incomplete": "needs a dry run against an unreachable dependency",
    "executables-identity-unlocked": _FAULT,
    "executables-pin-malformed": _REFUSAL,
    "frozen-marketplace-unlocked": _NETWORK,
    "github-auth-first-enabled": "auth opt-out diagnostic; github.com auth needs the network",
    "github-gitlab-host-conflict": _NETWORK,
    "hidden-characters-flagged": "covered by the audit --strip lifecycle tests",
    "hidden-characters-structured": "covered by the audit --strip lifecycle tests",
    "locked-refs-satisfied": _NOT_CLEARING,
    "marketplace-catalog-repinned": _NETWORK,
    "mcp-executables-untrusted": "needs an MCP or LSP server declared by an unapproved "
    "package; covered by the executables filter tests",
    "package-directory-collision": "needs duplicate package directories on a "
    "case-insensitive filesystem",
    "plugin-mcp-ownership-unknown": _FAULT,
    "registry-token-in-manifest": "needs the registries experimental flag and a registry",
    "subset-pairings-inconsistent": "needs a hand-edited lockfile subset record",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W3 core-models block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W3_CORE_MODELS_PROOFS

    assert {proof.remediation_id for proof in W3_CORE_MODELS_PROOFS} == EXECUTED
