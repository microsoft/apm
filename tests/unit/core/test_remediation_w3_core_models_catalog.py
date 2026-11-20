"""Catalog remediations printed by core/, models/, security/, utils/, output/,
update_policy.py and drift.py are true (#4090).

Every entry in the W3 core-models block of ``core/remediation.py`` names the
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

from apm_cli.core.remediation import catalog
from tests.utils.click_tree import resolve_line

_OWNER = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "core" / "remediation.py"
_BLOCK = re.compile(
    r"# -- W3 core-models --\n(?P<body>.*?)# -- end W3 core-models --",
    re.S,
)

# Executed installed-CLI proofs: test_w3_core_models_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "deployment-owners-invalid",
        "local-files-collide",
    }
)

_NETWORK = "needs a remote registry, marketplace, Azure DevOps or GitLab host"
_REFUSAL = "a parse-time refusal; the reported state is the invalid manifest or lockfile value"
_FAULT = "needs a filesystem or lockfile fault the hermetic world cannot induce cheaply"
_NOT_CLEARING = "an informational footer the reporting command always prints"

COMPONENT_ONLY: dict[str, str] = {
    "ado-coordinates-mismatched": _NETWORK,
    "claude-plugin-layout": "a pack-time refusal; the advised command writes another bundle "
    "format rather than clearing a project state",
    "compile-dry-run": _NOT_CLEARING,
    "dependency-alias-invalid": _REFUSAL,
    "dry-run-incomplete": "needs a dry run against an unreachable dependency",
    "executables-identity-unlocked": _FAULT,
    "executables-pin-malformed": _REFUSAL,
    "frozen-marketplace-unlocked": _NETWORK,
    "github-auth-first-enabled": "auth opt-out diagnostic; github.com auth needs the network",
    "github-gitlab-host-conflict": _NETWORK,
    "hidden-characters-flagged": "covered by the audit --strip lifecycle tests",
    "hidden-characters-structured": "covered by the audit --strip lifecycle tests",
    "locked-refs-satisfied": _NOT_CLEARING,
    "marketplace-catalog-repinned": _NETWORK,
    "mcp-executables-untrusted": "needs an MCP or LSP server declared by an unapproved "
    "package; covered by the executables filter tests",
    "package-directory-collision": "needs duplicate package directories on a "
    "case-insensitive filesystem",
    "plugin-mcp-ownership-unknown": _FAULT,
    "registry-token-in-manifest": "needs the registries experimental flag and a registry",
    "subset-pairings-inconsistent": "needs a hand-edited lockfile subset record",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W3 core-models block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_every_w3_core_models_remediation_has_a_proof_level() -> None:
    ids = set(_IDS)
    assert EXECUTED.isdisjoint(COMPONENT_ONLY)
    assert ids == EXECUTED | set(COMPONENT_ONLY), (
        sorted(ids - EXECUTED - set(COMPONENT_ONLY)),
        sorted((EXECUTED | set(COMPONENT_ONLY)) - ids),
    )


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W3_CORE_MODELS_PROOFS

    assert {proof.remediation_id for proof in W3_CORE_MODELS_PROOFS} == EXECUTED


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize("remediation_id", _IDS)
def test_w3_core_models_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
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
