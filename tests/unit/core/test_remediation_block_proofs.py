"""Every area block of ``core/remediation.py`` keeps its catalog promises (#4090).

Each block module (``test_remediation_<area>_catalog.py``) owns its proof
levels: :data:`EXECUTED` ids run end to end in
``tests/integration/test_remediation_advice_lifecycle.py`` and
:data:`COMPONENT_ONLY` ids carry the reason an executed proof is out of reach.
This module runs the checks shared by every block once: each block id has
exactly one proof level, and each entry renders a command line that resolves
in the real Click tree and names the state it clears.
"""

from __future__ import annotations

import re
from types import ModuleType

import pytest

from apm_cli.core.remediation import catalog
from tests.unit.core import (
    test_remediation_commands_catalog,
    test_remediation_w2_bundle_catalog,
    test_remediation_w2_integration_catalog,
    test_remediation_w2_marketplace_catalog,
    test_remediation_w3_core_models_catalog,
    test_remediation_w3_deps_compilation_catalog,
    test_remediation_w3_plugins_runtime_catalog,
    test_remediation_w4_drift_trust_catalog,
    test_remediation_w4_frozen_policy_catalog,
    test_remediation_w4_phases_targets_catalog,
)
from tests.utils.click_tree import resolve_line

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]

# The commands block keeps its own, stricter proof-level test.
_PROOF_BLOCKS: tuple[ModuleType, ...] = (
    test_remediation_w2_bundle_catalog,
    test_remediation_w2_integration_catalog,
    test_remediation_w2_marketplace_catalog,
    test_remediation_w3_core_models_catalog,
    test_remediation_w3_deps_compilation_catalog,
    test_remediation_w3_plugins_runtime_catalog,
    test_remediation_w4_drift_trust_catalog,
    test_remediation_w4_frozen_policy_catalog,
    test_remediation_w4_phases_targets_catalog,
)
_BLOCK_IDS = tuple(
    (block.__name__.rsplit(".", 1)[-1], remediation_id)
    for block in (test_remediation_commands_catalog, *_PROOF_BLOCKS)
    for remediation_id in block._IDS
)


@pytest.mark.parametrize("block", _PROOF_BLOCKS, ids=lambda block: block.__name__.rsplit(".")[-1])
def test_every_block_remediation_has_a_proof_level(block: ModuleType) -> None:
    ids = set(block._IDS)
    component_only = set(block.COMPONENT_ONLY)
    assert block.EXECUTED.isdisjoint(component_only)
    assert ids == block.EXECUTED | component_only, (
        sorted(ids - block.EXECUTED - component_only),
        sorted((block.EXECUTED | component_only) - ids),
    )


def _sample(text: str) -> str:
    text = re.sub(r"\{[a-z_]+\}", "sample", text)
    return re.sub(r"<[^>\s]+>", "sample", text)


@pytest.mark.parametrize(("block", "remediation_id"), _BLOCK_IDS)
def test_block_remediation_resolves_and_names_its_state(block: str, remediation_id: str) -> None:
    entry = catalog()[remediation_id]
    resolution = resolve_line(_sample(entry.command))
    assert resolution.ok, (block, entry.command, resolution)

    promise = entry.fixes_promise
    assert promise and promise == promise.strip()
    assert promise[0].islower() and not promise.startswith(("to ", "run ")), promise
    assert promise.isascii() and not promise.endswith(".")
    rendered = entry.bind(**{name: "sample" for name in re.findall(r"\{([a-z_]+)\}", repr(entry))})
    assert rendered.sentence().startswith(("Run '", rendered.sentence()[:1]))
    assert f"'{rendered.command}' to {rendered.fixes_promise}" in rendered.sentence()


# A lock reference in a manual step: the lockfile name, its placeholder or a lock entry.
_LOCK = r"(?:apm\.lock\.yaml|\{lockfile\}|\blockfile\b|\block entry\b)"
# Rule B (RA-004): no remedy hand-edits the lock, in any clause of a manual step.
_LOCK_EDIT = re.compile(rf"\b(?:remove|delete|edit|fix|drop|change)\b[^,;]*{_LOCK}", re.I)
_LOCK_MOVE_ASIDE = re.compile(rf"\bmove\b[^,;]*{_LOCK}[^,;]*\baside\b", re.I)
# Whole-lock quarantine only where the trigger makes the lock unusable; never
# for an integrity refusal (content hash, require_hashes, hash pins, provenance).
_MOVE_ASIDE_ALLOWED = frozenset(
    {
        "claude-plugin-binding-moved",
        "lockfile-unreadable",
        "lockfile-version-unsupported",
        "plugin-mcp-ownership-unknown",
    }
)


@pytest.mark.parametrize("remediation_id", sorted(catalog()))
def test_no_remediation_hand_edits_the_lockfile(remediation_id: str) -> None:
    entry = catalog()[remediation_id]
    for step in (entry.before, entry.then):
        if not step:
            continue
        assert not _LOCK_EDIT.search(step), (remediation_id, step)
        if _LOCK_MOVE_ASIDE.search(step):
            assert remediation_id in _MOVE_ASIDE_ALLOWED, (remediation_id, step)


def test_lock_move_aside_allowlist_is_live() -> None:
    moved = {
        remediation_id
        for remediation_id, entry in catalog().items()
        if any(_LOCK_MOVE_ASIDE.search(step or "") for step in (entry.before, entry.then))
    }
    assert moved == _MOVE_ASIDE_ALLOWED


# RA-008: 'apm install --force' and 'apm unpack --force' also deploy despite
# critical security findings, so no remedy offers them. The one --force left
# only overwrites the apm.yml marketplace block (marketplace/migration.py:264).
_FORCE_ALLOWED = frozenset({"marketplace-config-duplicate"})


def _remedy_text(entry) -> str:
    return " ".join(filter(None, (entry.command, entry.fixes_promise, entry.before, entry.then)))


def test_no_remediation_offers_force() -> None:
    forced = {
        remediation_id
        for remediation_id, entry in catalog().items()
        if "--force" in _remedy_text(entry)
    }
    assert forced == _FORCE_ALLOWED
    for remediation_id, entry in catalog().items():
        text = _remedy_text(entry)
        assert not re.search(r"\b(?:install|unpack)\b[^,;]*--force", text), (remediation_id, text)


# RA-015 (G7): no remedy advertises a policy bypass.
@pytest.mark.parametrize("remediation_id", sorted(catalog()))
def test_no_remediation_advertises_a_policy_bypass(remediation_id: str) -> None:
    text = _remedy_text(catalog()[remediation_id])
    for token in ("--no-policy", "APM_POLICY_DISABLE"):
        assert token not in text, (remediation_id, text)


# G8: every "Kept ..." line about a file APM left in place renders through
# remediation.kept_note / kept_file_message. These modules format kept lines
# that are not about a file; the set may only shrink.
_KEPT_LINE_ALLOWED = {
    "adapters/client/opencode.py": "an OpenCode config entry, not a file",
    "core/command_logger.py": "kept packages under --frozen, not a file",
    "install/mcp/retired_destinations.py": "an MCP server entry, not a file",
    "install/mcp/write_receipts.py": "an MCP server entry, not a file",
    "marketplace/reconcile.py": "kept marketplaces, not a file",
}


def _formats_kept_line(node) -> bool:
    import ast

    if isinstance(node, ast.JoinedStr) and node.values:
        node = node.values[0]
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("Kept ")
    )


def test_only_the_remediation_owner_formats_kept_lines() -> None:
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
    owners = set()
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        if relative == "core/remediation.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nested = {
            id(value)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            for value in node.values
        }
        if any(
            _formats_kept_line(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr | ast.Constant) and id(node) not in nested
        ):
            owners.add(relative)
    assert owners == set(_KEPT_LINE_ALLOWED)
