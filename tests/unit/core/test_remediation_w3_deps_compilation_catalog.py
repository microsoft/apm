"""Catalog remediations printed by ``deps/`` and ``compilation/`` are true (#4090).

Every entry in the W3 deps-compilation block of ``core/remediation.py`` names
the state it clears and renders a command line that resolves in the real Click
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
_BLOCK = re.compile(
    r"# -- W3 deps-compilation --\n(?P<body>.*?)# -- end W3 deps-compilation --",
    re.S,
)

# Executed installed-CLI proofs: test_area_remediation_clears_the_reported_state.
EXECUTED = frozenset({"orphaned-agents-md", "root-context-hand-authored"})

_LOCK_CONFLICT = (
    "needs two manifests pinning one package to different commits of a live remote; "
    "covered by the immutable requirements unit tests"
)

COMPONENT_ONLY: dict[str, str] = {
    "frozen-lockfile-stale": "needs a lockfile row hand-edited away from the resolved graph; "
    "covered by the immutable requirements unit tests",
    "immutable-requirements-conflict": _LOCK_CONFLICT,
    "immutable-requirements-conflict-frozen": _LOCK_CONFLICT,
    "nested-repository-skipped": "needs a nested Git worktree; covered by "
    "test_compile_clean_nested_worktree.py",
    "protocol-fallback-disallowed": "needs a strict clone failure against a real git remote",
    "root-instructions-hand-authored": "needs a hand-authored Copilot root instructions file "
    "under a copilot target; same compile loop as root-context-hand-authored",
    "single-agents-compilation": "alternative mode named in the distributed compile summary, "
    "not a reported state",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W3 deps-compilation block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import (
        W3_DEPS_COMPILATION_PROOFS,
    )

    assert {proof.remediation_id for proof in W3_DEPS_COMPILATION_PROOFS} == EXECUTED
