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

from apm_cli.core.remediation import catalog
from tests.utils.click_tree import resolve_line

_OWNER = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "core" / "remediation.py"
_BLOCK = re.compile(
    r"# -- W2 marketplace --\n(?P<body>.*?)# -- end W2 marketplace --",
    re.S,
)

# Executed installed-CLI proofs: test_w2_marketplace_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "marketplace-reconcile-failed",
        "marketplaces-outside-user-manifest",
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
    "marketplace-register-manual": "alternative to marketplaces-outside-user-manifest; "
    "manual registration is covered by the marketplace add tests",
    "marketplace-unregistered-shorthand": _NETWORK,
    "marketplace-unregistered-url": _NETWORK,
    "marketplace-yml-compile": _TEMPLATE,
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W2 marketplace block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_every_w2_marketplace_remediation_has_a_proof_level() -> None:
    ids = set(_IDS)
    assert EXECUTED.isdisjoint(COMPONENT_ONLY)
    assert ids == EXECUTED | set(COMPONENT_ONLY), (
        sorted(ids - EXECUTED - set(COMPONENT_ONLY)),
        sorted((EXECUTED | set(COMPONENT_ONLY)) - ids),
    )


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W2_MARKETPLACE_PROOFS

    assert {proof.remediation_id for proof in W2_MARKETPLACE_PROOFS} == EXECUTED


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize("remediation_id", _IDS)
def test_w2_marketplace_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
    entry = catalog()[remediation_id]
    resolution = resolve_line(_sample(entry.command))
    assert resolution.ok, (entry.command, resolution)

    promise = entry.fixes_promise
    assert promise and promise == promise.strip()
    assert promise[0].islower() and not promise.startswith(("to ", "run ")), promise
    assert promise.isascii() and not promise.endswith(".")
    rendered = entry.bind(**{name: "sample" for name in re.findall(r"\{([a-z_]+)\}", repr(entry))})
    assert rendered.sentence().startswith(("Run '", rendered.sentence()[:1]))
    assert f"'{rendered.command}' to {rendered.fixes_promise}" in rendered.sentence()
