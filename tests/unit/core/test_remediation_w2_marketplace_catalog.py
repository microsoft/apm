"""Catalog remediations printed by ``src/apm_cli/marketplace`` are true (#4090).

Every entry in the W2 marketplace block of ``core/remediation.py`` names the
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
_BLOCK = re.compile(
    r"# -- W2 marketplace --\n(?P<body>.*?)# -- end W2 marketplace --",
    re.S,
)

# Executed installed-CLI proofs: test_area_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "marketplace-reconcile-failed",
    }
)

_CONFIRM = (
    "the advised 'apm marketplace remove' asks for confirmation; the installed-CLI "
    "runner has no stdin to answer it"
)
_NETWORK = "runs against a remote Git host or GitHub"
_GIT_FAULT = "needs a Git access or URL-rewrite fault the hermetic world cannot induce"
_TEMPLATE = "advice is a comment in a scaffolded file, not a reported state"

COMPONENT_ONLY: dict[str, str] = {
    "marketplace-block-compile": _TEMPLATE,
    "marketplace-duplicate-alias": _CONFIRM,
    "marketplace-fetch-failed": "the reported fetch failure persists until its cause is "
    "fixed; the retry alone does not clear it",
    "marketplace-git-access-failed": _GIT_FAULT,
    "marketplace-git-rewrite-rejected": _GIT_FAULT,
    "marketplace-host-conflict": _NETWORK,
    "marketplace-manual-kept": _CONFIRM,
    "marketplace-origin-restore": "needs a lockfile recorded against a marketplace whose "
    "registration later moved; covered by the marketplace origin lifecycle tests",
    "marketplace-origin-switch": "needs a lockfile recorded against a marketplace whose "
    "registration later moved; covered by the marketplace origin lifecycle tests",
    "marketplace-origin-switch-global": "user-scope form of marketplace-origin-switch; "
    "full sentence asserted by test_conflict_remedy_names_the_refused_scope",
    "marketplace-unregistered-shorthand": _NETWORK,
    "marketplace-unregistered-url": _NETWORK,
    "marketplace-yml-compile": _TEMPLATE,
    "marketplace-origin-readd": "full sentence asserted by test_remediation_gate9_call_sites; test_marketplace_origin_entry_points_lifecycle reaches the refusal",
    "marketplace-origin-reinstall": "full sentence asserted by test_remediation_gate9_call_sites; test_marketplace_origin_entry_points_lifecycle reaches the refusal",
    "marketplace-project-origin-relock": "installed-CLI refusal asserted by "
    "test_root_map_wins_over_a_lock_written_through_a_registration (needs a remote catalog "
    "locked through a registration, then a committed map naming another origin)",
    "marketplace-allow-readd": "installed-CLI remedy executed by "
    "test_marketplace_allow_judges_locked_and_declared_origins (needs a served org policy)",
    "marketplace-credential-bind": "needs a loopback HTTPS catalog that denies an unbound "
    "registration; the bound flow is executed by test_marketplace_hosted_catalog_auth_lifecycle",
    "marketplace-credential-rebind": _CONFIRM,
    "marketplace-ref-swap-accept": "test_marketplace_ref_swap_lifecycle asserts the printed next step; full sentence in test_remediation_gate9_call_sites",
    "marketplace-ref-swap-accept-install": "installed-CLI remedy executed by "
    "test_a_different_commit_is_refused_and_the_printed_install_accepts_it (#5526)",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W2 marketplace block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W2_MARKETPLACE_PROOFS

    assert {proof.remediation_id for proof in W2_MARKETPLACE_PROOFS} == EXECUTED
