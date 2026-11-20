"""Catalog remediations printed by ``src/apm_cli/commands`` are true (#4090).

Every entry in the commands/** block of ``core/remediation.py`` names the state
it clears and renders a command line that resolves in the real Click tree.
Entries whose state an installed-CLI test can induce are executed end to end in
``tests/integration/test_remediation_advice_lifecycle.py``
(:data:`EXECUTED`); the rest are listed in :data:`COMPONENT_ONLY` with the
reason an executed proof is out of reach.
"""

from __future__ import annotations

import pytest

from tests.utils.remediation_proofs import assert_component_proof, assert_proof_levels, block_ids

# Executed installed-CLI proofs: test_command_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "dependencies-not-installed",
        "executables-approved",
        "executables-denied",
        "executables-unapproved",
        "installed-files-differ",
        "lockfile-missing-lock",
        "lockfile-unreadable",
        "orphaned-packages-prune",
        "project-manifest-missing",
        "project-owned-file",
        "user-manifest-missing",
    }
)

_FAULT = "needs a filesystem or registration fault the hermetic world cannot induce"
_NETWORK = "runs against the network or a real installer"
_OUTSIDE = "advised by a command outside the executed command families"

COMPONENT_ONLY: dict[str, str] = {
    "agent-plugin-bundle-consumer": _OUTSIDE,
    "auth-token-unexported": _NETWORK,
    "bundle-target-mismatch": _OUTSIDE,
    "claude-plugin-registration-stale": _FAULT,
    "cli-binary-outdated": _NETWORK,
    "cli-installer-unreachable": _NETWORK,
    "compiled-outputs-orphaned": _OUTSIDE,
    "copilot-plugin-registration-stale": _FAULT,
    "dependency-unplaced": "needs a lockfile row the resolver cannot place; not reachable "
    "from a consistent install",
    "deployment-records-stale": "needs a lockfile written before canonical deployment records",
    "deps-update-deprecated": "deprecation redirect; the reported state is the command name",
    "experimental-flag-disabled": _OUTSIDE,
    "experimental-flags-unknown": _OUTSIDE,
    "hook-config-stale": _FAULT,
    "init-targets-undetected": _OUTSIDE,
    "integrated-files-stale": _FAULT,
    "integrations-unsynced": _FAULT,
    "integrations-unsynced-after-fix": _FAULT,
    "legacy-bundle-format": _OUTSIDE,
    "local-dependency-unmapped": "needs a hand-edited lockfile local key",
    "local-dependency-unresolved": _FAULT,
    "lockfile-records-inconsistent": "needs duplicate or invalid lockfile rows",
    "lockfile-state-unreconciled": _FAULT,
    "marketplace-add-failed": _OUTSIDE,
    "marketplace-build-removed": _OUTSIDE,
    "marketplace-config-duplicate": _OUTSIDE,
    "marketplace-config-legacy": _OUTSIDE,
    "marketplace-config-missing": _OUTSIDE,
    "marketplace-json-missing": _OUTSIDE,
    "marketplace-ref-unresolved": _NETWORK,
    "marketplace-search-format": _OUTSIDE,
    "marketplaces-none-registered": _OUTSIDE,
    "mcp-dependency-declared": "uninstall refuses before any state changes; covered by the "
    "uninstall MCP refusal tests",
    "mcp-entry-stale": _FAULT,
    "mcp-registry-url-override": _OUTSIDE,
    "outside-constraint-release": _NETWORK,
    "prune-incomplete": _FAULT,
    "prune-partial": _FAULT,
    "registry-canonical-unrecorded": _NETWORK,
    "runtime-missing": _NETWORK,
    "staged-bundle-drift": _OUTSIDE,
    "target-not-deployed": _OUTSIDE,
    "user-instructions-missing": _OUTSIDE,
    "user-manifest-invalid": _OUTSIDE,
    "user-packages-missing": _OUTSIDE,
    "lifecycle-scripts-untrusted": _OUTSIDE,
}


_IDS = block_ids("commands/** (W1)")


def test_every_commands_remediation_has_a_proof_level() -> None:
    assert_proof_levels("commands/** (W1)", EXECUTED, COMPONENT_ONLY)


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import TEXT_PROOFS

    assert {proof.remediation_id for proof in TEXT_PROOFS} == EXECUTED


@pytest.mark.parametrize("remediation_id", _IDS)
def test_commands_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
    assert_component_proof(remediation_id)
