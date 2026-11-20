"""Catalog remediations printed by policy/ and the frozen-install, integrity,
local-edit and content-hash modules under install/ are true (#4090).

Every entry in the W4 frozen-policy block of ``core/remediation.py`` names the
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
    r"# -- W4 frozen-policy --\n(?P<body>.*?)# -- end W4 frozen-policy --",
    re.S,
)

# Executed installed-CLI proofs: test_w4_frozen_policy_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "dependency-selection-changed",
        "frozen-lock-state-drift",
        "frozen-selections-changed",
    }
)

_FAULT = "needs a lockfile or filesystem fault the hermetic world cannot induce cheaply"
_POLICY = "needs an org policy; the hermetic world installs with --no-policy"
_BUNDLE = "needs a local bundle with hooks and an executables trust change"

COMPONENT_ONLY: dict[str, str] = {
    "bundle-hooks-approved": _BUNDLE,
    "bundle-hooks-unadmitted": _BUNDLE,
    "content-hash-unverified": "needs a legacy (version 1-3) lockfile whose carried bytes "
    "cannot be verified",
    "content-hashes-missing": _POLICY,
    "deployment-ownership-missing": _FAULT,
    "drift-cache-unpopulated": "audit replay re-fetches a missing remote package; only a "
    "missing local source misses, and install cannot restore that",
    "frozen-local-content-changed": "needs a frozen local-path dependency; covered by the "
    "frozen local content unit tests",
    "frozen-hook-pins-stale": "covered by the hook approval pin parity lifecycle test, "
    "which retires the stale hook with a plain install",
    "frozen-lockfile-withheld": "covered by the frozen deployed-hash lifecycle test, which "
    "re-renders a locked file with an older APM rendering",
    "frozen-operation-rejected": "usage refusal; the reported state is the flag combination, "
    "not project state",
    "local-dependency-graph-corrupt": "needs a hand-edited lockfile resolved_by chain",
    "local-edits-export": "preserves local edits as patches; it does not clear the edit, "
    "the following install does",
    "managed-paths-unresolved": _FAULT,
    "rendering-changed": "needs a locked file rendered by an older APM version",
    "required-package-unlocked": _POLICY,
    "unmanaged-file-track": _POLICY,
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W4 frozen-policy block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W4_FROZEN_POLICY_PROOFS

    assert {proof.remediation_id for proof in W4_FROZEN_POLICY_PROOFS} == EXECUTED
