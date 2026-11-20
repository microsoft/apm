"""Catalog remediations printed by ``src/apm_cli/integration`` are true (#4090).

Every entry in the W2 integration block of ``core/remediation.py`` names the
state it clears and renders a command line that resolves in the real Click
tree. Entries whose state an installed-CLI test can induce are executed end to
end in ``tests/integration/test_remediation_advice_lifecycle.py``
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
_BLOCK = re.compile(r"# -- W2 integration --\n(?P<body>.*?)# -- end W2 integration --", re.S)

# Executed installed-CLI proofs: test_w2_integration_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "audit-target-flag-disabled",
        "hook-sidecar-unreadable",
        "skill-selection-unmatched",
    }
)

_SOURCE = "the manual step edits the published package source, which needs a new revision"
_FAULT = "needs a filesystem fault the hermetic world cannot induce"
_COWORK = "needs the copilot-cowork flag and a OneDrive path the hermetic world cannot provide"
_NETWORK = "needs an MCP registry lookup"

COMPONENT_ONLY: dict[str, str] = {
    "canvas-extensions-blocked": "needs a published Copilot canvas extension and the "
    "copilot-app flag; approval itself is covered by the executables proofs",
    "codex-agent-claude-model": _SOURCE,
    "codex-agent-field-not-string": _SOURCE,
    "codex-agent-frontmatter-invalid": _SOURCE,
    "codex-agent-frontmatter-not-mapping": _SOURCE,
    "copilot-app-workflow-unbound": "needs the Copilot App workflow store outside the project",
    "cowork-skills-dir-undetected": _COWORK,
    "hook-config-unreadable": "same dropped-target cleanup as hook-sidecar-unreadable, which "
    "is executed",
    "hook-path-escapes-package": _SOURCE,
    "hook-plugin-root-unresolved": _SOURCE,
    "instruction-frontmatter-rejected": _SOURCE,
    "local-file-unmanaged": "install renders file and skill skips through the diagnostics "
    "summary; the canvas collision needs a published canvas extension",
    "mcp-admitted-denied": "needs a policy deny over an admitted MCP server",
    "mcp-admitted-unapproved": "needs a lockfile admission the manifest approvals no longer hold",
    "mcp-local-package-unresolved": "needs a lockfile local key that no longer resolves",
    "mcp-lockfile-unwritable": _FAULT,
    "mcp-package-manifest-missing": "needs an MCP-only package whose cached manifest vanished "
    "outside the cold-cache restore path",
    "mcp-registry-alternative": _NETWORK,
    "skill-conflict-unmanaged": "needs an unmanaged skill that differs from the package copy; "
    "the rename is the manual step",
    "skill-selection-declared-unmatched": "needs a transitive package whose own apm.yml "
    "declares the selection; updating it needs a new revision",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W2 integration block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W2_INTEGRATION_PROOFS

    assert {proof.remediation_id for proof in W2_INTEGRATION_PROOFS} == EXECUTED
