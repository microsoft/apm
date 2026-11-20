"""Catalog remediations printed by MCP client adapters, native plugin registrars
and compilation are true (#4090, W3: G5).

Every entry in the W3: G5 block of ``core/remediation.py`` names the state it
clears and renders a command line that resolves in the real Click tree.
Entries whose state an installed-CLI test can induce are executed end to end in
``tests/integration/test_remediation_w3_g5_lifecycle.py`` (:data:`EXECUTED`);
the rest are listed in :data:`COMPONENT_ONLY` with the reason an executed
proof is out of reach.
"""

from __future__ import annotations

import pytest

from tests.utils.remediation_proofs import assert_component_proof, assert_proof_levels, block_ids

_BLOCK = "W3: G5"

# Executed installed-CLI proofs: test_g5_remediation_clears_the_reported_state.
EXECUTED = frozenset({"hand-authored-output-protected", "orphaned-agents-files"})

_IDE_FAULT = (
    "needs an unreadable, malformed or unwritable JetBrains MCP config under the real "
    "user config root"
)
_REGISTRY = "needs a live MCP registry server that declares required variables"
_CODEX = "needs a real Codex runtime that owns the native plugin cache and marketplace"
_CLAUDE = "needs user-scope native Claude plugin state recorded by a prior global install"

COMPONENT_ONLY: dict[str, str] = {
    "mcp-client-config-unreadable": _IDE_FAULT,
    "mcp-client-config-malformed": _IDE_FAULT,
    "mcp-client-config-unwritable": _IDE_FAULT,
    "mcp-required-variables-unset": _REGISTRY,
    "codex-plugin-provenance-missing": _CODEX,
    "codex-plugin-cache-differs": _CODEX,
    "codex-plugin-registration-stale": _CODEX,
    "codex-plugin-project-scope": _CODEX,
    "claude-plugin-source-missing": _CLAUDE,
    "claude-plugin-content-differs": _CLAUDE,
    "claude-plugin-marketplace-moved": _CLAUDE,
    "claude-plugin-bin-untrusted": "needs a user-scope native Claude plugin that ships "
    "bin/ executables",
    "copilot-settings-invalid": "needs a malformed GitHub Copilot settings file under the "
    "real user config root",
    "agent-plugin-legacy-package": "producer-side repack of a third-party plugin source; "
    "the consumer cannot observe the publisher's package change",
    "nested-repository-skipped": "needs a nested Git repository whose own project compiles "
    "independently of the reporting one",
    "agents-files-distributed": "compilation-mode suggestion in every distributed "
    "compile summary, so no later probe observes it cleared",
}

_IDS = block_ids(_BLOCK)


def test_every_g5_remediation_has_a_proof_level() -> None:
    assert_proof_levels(_BLOCK, EXECUTED, COMPONENT_ONLY)


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_w3_g5_lifecycle import G5_PROOFS

    assert {proof.remediation_id for proof in G5_PROOFS} == EXECUTED


@pytest.mark.parametrize("remediation_id", _IDS)
def test_g5_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
    assert_component_proof(remediation_id)
