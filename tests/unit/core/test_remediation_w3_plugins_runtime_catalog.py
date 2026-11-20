"""Catalog remediations printed by the plugins and runtime modules are true (#4090).

Every entry in the W3 plugins-runtime block of ``core/remediation.py`` (advice
from adapters/, agent_plugins/, claude_plugins/, codex_plugins/,
copilot_plugins/ and runtime/) names the state it clears and renders a command
line that resolves in the real Click tree. Entries whose state an installed-CLI
test can induce are executed end to end in
``tests/integration/test_remediation_advice_lifecycle.py`` (:data:`EXECUTED`);
the rest are listed in :data:`COMPONENT_ONLY` with the reason an executed proof
is out of reach.
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
    r"# -- W3 plugins-runtime --\n(?P<body>.*?)# -- end W3 plugins-runtime --",
    re.S,
)

# Executed installed-CLI proofs: test_area_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "copilot-settings-invalid",
        "jetbrains-mcp-config-unusable",
    }
)

_CLAUDE = "needs a user-scope native Claude plugin registration and Claude settings"
_CODEX = "needs the Codex CLI app-server at or above the qualified minimum"
_REGISTRY = "needs an MCP server resolved from the network registry"

COMPONENT_ONLY: dict[str, str] = {
    "agent-plugin-bundle-route": "needs an Agent Plugins bundle on the imperative local-bundle "
    "route; covered by the agent plugin deployment boundary tests",
    "agent-plugin-legacy-repack": "the remediation is on the publisher side: it repacks a "
    "package, it does not change the consumer state that reported it",
    "claude-plugin-bin-untrusted": _CLAUDE,
    "claude-plugin-binding-moved": _CLAUDE,
    "claude-plugin-source-stale": _CLAUDE,
    "codex-native-cache-differs": _CODEX,
    "codex-native-registration-unverified": _CODEX,
    "codex-plugin-user-scope": _CODEX,
    "codex-provenance-reinstall": _CODEX,
    "codex-provenance-uninstall": _CODEX,
    "llm-tls-bootstrap-missing": "runs a real runtime installer against the network",
    "mcp-container-option-unresolved": _REGISTRY,
    "mcp-env-vars-unresolved": _REGISTRY,
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W3 plugins-runtime block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W3_PLUGINS_RUNTIME_PROOFS

    assert {proof.remediation_id for proof in W3_PLUGINS_RUNTIME_PROOFS} == EXECUTED
