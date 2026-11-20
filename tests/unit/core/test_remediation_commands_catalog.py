"""Catalog remediations printed by ``src/apm_cli/commands`` are true (#4090).

Every entry in the commands/** block of ``core/remediation.py`` names the state
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
_BLOCK = re.compile(
    r"# -- commands/\*\* \(W1\) -+\n(?P<body>.*?)# -- end commands/\*\* \(W1\) -+",
    re.S,
)

# Executed installed-CLI proofs: test_command_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "dependencies-not-installed",
        "executables-approved",
        "executables-denied",
        "executables-unapproved",
        "hidden-characters-found",
        "hidden-characters-previewed",
        "installed-files-differ",
        "lockfile-missing-lock",
        "lockfile-unreadable",
        "orphaned-packages-prune",
        "project-manifest-bootstrap",
        "project-manifest-missing",
    }
)

_FAULT = "needs a filesystem or registration fault the hermetic world cannot induce"
_NETWORK = "runs against the network or a real installer"
_OUTSIDE = "advised by a command outside the executed command families"

COMPONENT_ONLY: dict[str, str] = {
    "bundle-hooks-unchecked": (
        "needs target resolution to fail mid-prune; tests/unit/test_prune_command.py "
        "drives that branch"
    ),
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
    "project-owned-file": "editing the project source is the manual step; covered by the "
    "view --file provenance tests",
    "prune-incomplete": _FAULT,
    "prune-partial": _FAULT,
    "registry-canonical-unrecorded": _NETWORK,
    "runtime-missing": _NETWORK,
    "staged-bundle-drift": _OUTSIDE,
    "target-not-deployed": _OUTSIDE,
    "user-instructions-missing": _OUTSIDE,
    "user-manifest-invalid": _OUTSIDE,
    "user-manifest-missing": "installs a user-scope package; covered by the global lifecycle tests",
    "user-packages-missing": _OUTSIDE,
    "lifecycle-scripts-untrusted": _OUTSIDE,
    "frozen-update-conflict": "usage refusal; the reported state is the flag combination, "
    "not project state",
    "bundle-repack-plugin": "runs in the publisher's source project, not the consumer that "
    "reported the bundle",
    "legacy-bundle-unpack": _OUTSIDE,
    "registry-package-install": _NETWORK,
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "commands/** block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_every_commands_remediation_has_a_proof_level() -> None:
    ids = set(_IDS)
    assert EXECUTED.isdisjoint(COMPONENT_ONLY)
    assert ids == EXECUTED | set(COMPONENT_ONLY), (
        sorted(ids - EXECUTED - set(COMPONENT_ONLY)),
        sorted((EXECUTED | set(COMPONENT_ONLY)) - ids),
    )


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import TEXT_PROOFS

    assert {proof.remediation_id for proof in TEXT_PROOFS} == EXECUTED


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize("remediation_id", _IDS)
def test_commands_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
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
