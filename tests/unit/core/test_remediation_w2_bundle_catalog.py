"""Catalog remediations printed by ``src/apm_cli/bundle`` are true (#4090).

Every entry in the W2 bundle block of ``core/remediation.py`` names the state
it clears and renders a command line that resolves in the real Click tree.
Entries whose state an installed-CLI test can induce are executed end to end in
``tests/integration/test_remediation_advice_lifecycle.py``
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
_BLOCK = re.compile(r"# -- W2 bundle --\n(?P<body>.*?)# -- end W2 bundle --", re.S)

# Executed installed-CLI proofs: test_w2_bundle_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "pack-installed-file-tampered",
        "pack-installed-files-missing",
        "pack-lockfile-missing",
    }
)

COMPONENT_ONLY: dict[str, str] = {
    "agent-plugin-mcp-unportable": "the advised command packs a different format; it does "
    "not clear the agent-plugin refusal, it sidesteps it",
    "agent-plugin-surfaces-dropped": "the advised command packs a different format; it does "
    "not clear the agent-plugin refusal, it sidesteps it",
    "pack-dependency-unattested": "needs a lockfile row with no deployed files beside a "
    "populated apm_modules cache; not reachable from a consistent install",
    "pack-skill-subset-missing": "needs a skill-subset package whose recorded skills vanish "
    "from deployed files; covered by the plugin exporter skill-subset unit tests",
    "unpack-critical-findings": "needs a '--format apm' bundle carrying critical hidden "
    "characters; covered by the unpack security unit tests",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W2 bundle block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_every_bundle_remediation_has_a_proof_level() -> None:
    ids = set(_IDS)
    assert ids
    assert EXECUTED.isdisjoint(COMPONENT_ONLY)
    assert ids == EXECUTED | set(COMPONENT_ONLY), (
        sorted(ids - EXECUTED - set(COMPONENT_ONLY)),
        sorted((EXECUTED | set(COMPONENT_ONLY)) - ids),
    )


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W2_BUNDLE_PROOFS

    assert {proof.remediation_id for proof in W2_BUNDLE_PROOFS} == EXECUTED


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize("remediation_id", _IDS)
def test_bundle_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
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
