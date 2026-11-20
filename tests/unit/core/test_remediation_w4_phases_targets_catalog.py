"""Catalog remediations printed by install phases and target helpers are true (#4090).

Every entry in the W4 phases-targets block of ``core/remediation.py`` names the
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
    r"# -- W4 phases-targets --\n(?P<body>.*?)# -- end W4 phases-targets --",
    re.S,
)

# Executed installed-CLI proofs: test_w4_phases_targets_remediation_clears_the_reported_state.
EXECUTED = frozenset(
    {
        "agent-source-asset-skipped",
        "canvas-skipped-unapproved",
        "cowork-skills-dir-required",
        "local-package-content-invalid",
        "local-package-path-missing",
        "replacement-rules-partial-install",
    }
)

_FAULT = "needs a filesystem fault or race the hermetic world cannot induce"
_ROOT = "needs an unreadable file; the hermetic tests may run as root, which reads it anyway"
_CODEX = "needs a real Codex native plugin registration"
_HINT = "advisory hint printed after every qualifying install; compile does not suppress it"
_HASH = "needs upstream content to change under a locked commit (same-commit hash mismatch)"
_LEGACY_SKILLS = "needs legacy per-client skill deployments recorded by an older APM"

COMPONENT_ONLY: dict[str, str] = {
    "install-audit-force": "needs the external-scanners flag and a block audit mode; covered "
    "by the install audit policy floor tests",
    "codex-admission-stale": _CODEX,
    "codex-native-loose-files": _CODEX,
    "root-context-compile": _HINT,
    "user-root-context-compile": _HINT,
    "executables-parked": "printed only by the interactive executables approval prompt",
    "local-package-unreadable": _ROOT,
    "cowork-target-user-scope": "a user-scope Cowork skill deployment to a configured "
    "directory is refused by the containment check in the hermetic home",
    "agent-source-unreadable": _ROOT,
    "casing-migration-source-missing": _FAULT,
    "claude-native-switch-loose-hooks": "needs a real Claude native plugin switch over "
    "deployed loose hooks",
    "skill-path-migration-conflict": _LEGACY_SKILLS,
    "skill-path-migration-legacy": _LEGACY_SKILLS,
    "content-hash-cache-clean": _HASH,
    "content-hash-repin": _HASH,
    "content-hash-repin-global": _HASH,
    "materialized-manifest-invalid": "needs an acquired package whose apm.yml turns invalid "
    "after resolution",
    "hook-reconcile-incomplete": _FAULT,
    "hooks-skipped-unapproved": "install reports skipped hooks through exec_gate.log_hooks_skip; "
    "target_warnings.log_hooks_skip has no install caller",
}


def _block_ids() -> list[str]:
    match = _BLOCK.search(_OWNER.read_text(encoding="utf-8"))
    assert match, "W4 phases-targets block markers missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


_IDS = _block_ids()


def test_every_w4_phases_targets_remediation_has_a_proof_level() -> None:
    ids = set(_IDS)
    assert EXECUTED.isdisjoint(COMPONENT_ONLY)
    assert ids == EXECUTED | set(COMPONENT_ONLY), (
        sorted(ids - EXECUTED - set(COMPONENT_ONLY)),
        sorted((EXECUTED | set(COMPONENT_ONLY)) - ids),
    )


def test_executed_ids_match_the_installed_cli_proofs() -> None:
    from tests.integration.test_remediation_advice_lifecycle import W4_PHASES_TARGETS_PROOFS

    assert {proof.remediation_id for proof in W4_PHASES_TARGETS_PROOFS} == EXECUTED


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize("remediation_id", _IDS)
def test_w4_phases_targets_remediation_resolves_and_names_its_state(remediation_id: str) -> None:
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
