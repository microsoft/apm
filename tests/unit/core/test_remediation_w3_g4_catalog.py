"""Catalog remediations in the W3 G4 block are true (#4090).

Every entry in the ``W3: G4`` block of ``core/remediation.py`` (advice printed
by security/, core/, deps/, models/ and shared helpers) names the state it
clears and renders a command line that resolves in the real Click tree.
Entries whose state an installed-CLI test can induce are executed end to end
in ``tests/integration/test_remediation_w3_g4_lifecycle.py`` (:data:`EXECUTED`);
the rest are listed in :data:`COMPONENT_ONLY` with the reason an executed
proof is out of reach.
"""

from __future__ import annotations

import pytest

from tests.utils.remediation_proofs import assert_component_proof, assert_proof_levels, block_ids

_BLOCK = "W3: G4"

# Executed installed-CLI proofs: test_g4_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "agent-plugin-output-empty",
        "compile-dry-run",
        "dependency-alias-invalid",
        "deployment-owners-invalid",
        "executable-pin-invalid",
        "hidden-characters-flagged",
        "local-files-unmanaged",
        "plugin-mcp-ownership-unknown",
    }
)

_NETWORK = "runs against the network or a real installer"

COMPONENT_ONLY: dict[str, str] = {
    "ado-coordinates-mismatched": "needs an Azure DevOps host; the hermetic world serves "
    "GitHub-shaped URLs only",
    "catalog-selection-moved": "needs a marketplace catalog that re-points a locked "
    "catalog-metadata row; apm update also prompts unless stdin is a TTY",
    "dry-run-plan-incomplete": "needs a dry run whose transitive plan is unknown without "
    "network resolution",
    "executables-identity-unlocked": "needs an MCP or LSP declarer resolved without lock state, "
    "which a consistent install never produces",
    "executables-untrusted": "an installed package's MCP servers are parked by install with "
    "its own notice; this filter only sees declarers that pass outside that path",
    "frozen-lockfile-stale": "install's --frozen preflight reports a manifest/lockfile mismatch "
    "before the resolver refusals that print this advice",
    "github-auth-first-enabled": _NETWORK,
    "immutable-refs-conflict": "needs two manifests that pin one package to different commits "
    "through a transitive graph",
    "llm-tls-bootstrap-missing": _NETWORK,
    "locked-refs-pinned": "apm update prompts for confirmation unless stdin is a TTY",
    "package-directories-collide": "needs case-folding or duplicate package directories the "
    "case-sensitive hermetic filesystem cannot induce",
    "protocol-fallback-disallowed": _NETWORK,
    "registry-token-in-manifest": "needs the registries experimental flag and a package registry",
}


_IDS = block_ids(_BLOCK)


def test_every_g4_remediation_has_a_proof_level() -> None:
    assert_proof_levels(_BLOCK, EXECUTED, COMPONENT_ONLY)


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_w3_g4_lifecycle import G4_PROOFS

    assert {proof.remediation_id for proof in G4_PROOFS} == EXECUTED


@pytest.mark.parametrize("remediation_id", _IDS)
def test_g4_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
    assert_component_proof(remediation_id)
