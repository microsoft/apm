"""Rules, determinism and receipts of the lifecycle obligation generator (#4099).

Pins the steward rules (U1-U10, E1-E3) on the generated set, the evidence
anchors of every rule, the derived target parameters, the checked-in output
(no drift, stable hash) and the negative control: an obligation is credited
only by ``oracles.check_obligation``; without that call it stays uncovered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
import yaml

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.lifecycle import oracles, receipts
from tests.lifecycle.generator import bin_pin, emit, replay
from tests.lifecycle.generator.covering import refusal_line
from tests.lifecycle.generator.factors import (
    FACTORS,
    GATE_AUTHORITY,
    cell_id,
    compile_staged_targets,
    parse_cell,
)
from tests.lifecycle.generator.rules import (
    ACCEPTS_EVIDENCE,
    EVIDENCE_WINDOW,
    MARKETPLACE_ADD_REFUSAL,
    REFUSAL_EVIDENCE,
    RULE_CELL_REFUSALS,
    RULES,
    U_BIN_CELLS,
    U_BIN_PIN,
    UNINSTALL_MCP_REFUSALS,
    USER_SCOPE_EVIDENCE,
    classify,
    refusal_cell,
)
from tests.lifecycle.generator.space import build_space, raw_cells
from tests.lifecycle.generator.witnesses import implied, machine_cells
from tests.lifecycle.obligations import Obligation, promise_ids, uncovered
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.component,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I4",
        "P-INV-I7",
        "P-INV-I8",
        "P-OWN-executable-item-admission",
        "P-OWN-local-bundle-compile-staging",
        "P-OWN-frozen-install-mutation-eligibility",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]


@cache
def _space():
    return build_space()


@cache
def _generated() -> tuple[str, dict[str, str]]:
    return emit.generate()


def _document() -> dict:
    return json.loads(_generated()[0])


def _status(family: str, **cell: str):
    return classify(family, cell)


# -- U1 / U5: refusal cells ---------------------------------------------------


def test_install_mcp_with_target_is_a_success_cell() -> None:
    verdict = _status("pair", OP="mcp-install", SELECT="on")
    assert verdict.status == "obligation"
    key = cell_id("pair", {"OP": "mcp-install", "SELECT": "on"})
    assert _document()["obligations"][key]["family"] == "pair"


def test_frozen_with_update_is_one_refusal_cell_exiting_2() -> None:
    refusal = "refusal|OP=install|FROZEN=on|UPDATE=on"
    for family, cell in (
        ("pair", {"FROZEN": "on", "UPDATE": "on"}),
        ("pair", {"OP": "install", "FROZEN": "on", "UPDATE": "on"}),
    ):
        verdict = classify(family, cell)
        assert (verdict.status, verdict.maps_to) == ("mapped", refusal)
    entry = _space().obligations[refusal]
    assert (entry.family, entry.exit, entry.strength) == ("refusal", 2, "e2e-evidence")
    assert refusal_line(entry.cell) == "apm install --frozen --update"


def test_pack_global_is_a_refusal_and_pack_has_no_source_coordinate() -> None:
    assert _status("pair", OP="pack", SCOPE="user").maps_to == "refusal|OP=pack|SCOPE=user"
    assert all(_status("pair", OP="pack", SRC=src).rules == ("U6",) for src in FACTORS["SRC"])


def test_a_refusal_never_discharges_a_success_obligation() -> None:
    space = _space()
    for key, (target, _) in space.mapped.items():
        if target.startswith("refusal|"):
            assert classify(*_parse(key)).status == "mapped", key
    for row in _generated()[1].values():
        for step in _steps(yaml.safe_load(row)):
            if isinstance(step, dict) and "exit" in step:
                assert all(
                    cell.startswith("refusal|") or cell in RULE_CELL_REFUSALS
                    for cell in step.get("cells", ())
                ), step


# -- U2: the two deny levels never collapse ------------------------------------


def test_skill_gate_levels_map_but_the_dependency_deny_never_collapses() -> None:
    executable = _status("pair", PRIM="skill", GATE="denied-executable")
    assert executable.maps_to == "pair|PRIM=skill|GATE=none"
    dependency = _status("pair", PRIM="skill", GATE="denied-dependency")
    assert (dependency.status, dependency.must_cover, dependency.rules) == (
        "obligation",
        True,
        ("U2",),
    )
    assert GATE_AUTHORITY["denied-executable"] != GATE_AUTHORITY["denied-dependency"]
    space = _space()
    for key, (target, _) in space.mapped.items():
        if "GATE=denied-dependency" in key and not target.startswith("refusal|"):
            assert "GATE=denied-dependency" in target, (key, target)
    revoked = _status("pair", PRIM="skill", GATE="revoked-after-deploy")
    assert revoked.status == "obligation" and revoked.must_cover
    # other-file maps onto the same skill cell (E3), never onto GATE=none.
    assert space.mapped["pair|PRIM=other-file|GATE=revoked-after-deploy"][0] == (
        "pair|PRIM=skill|GATE=revoked-after-deploy"
    )


# -- U7, U8, U9, U10 ---------------------------------------------------------------


def test_mcp_install_of_another_primitive_keeps_no_collateral_write() -> None:
    verdict = _status("pair", OP="mcp-install", PRIM="skill")
    assert verdict.must_cover and verdict.rules == ("U7",)
    assert _space().obligations["pair|OP=mcp-install|PRIM=skill"].strength == "e2e-evidence"


def test_marketplace_add_accepts_git_a_local_path_and_file_urls() -> None:
    accepted = {"marketplace-git", "marketplace-local", "marketplace-file"}
    for src in FACTORS["SRC"]:
        verdict = _status("pair", OP="marketplace", SRC=src)
        assert (verdict.status == "obligation") == (src in accepted), src
    assert _status("pair", OP="bundle-install", SRC="git").rules == ("U8",)


def test_approve_without_a_decision_is_reachable_and_listing_is_read_only() -> None:
    verdict = _status("pair", OP="approve-deny", GATE="unapproved-exec")
    assert verdict.status == "obligation" and verdict.rules == ("U9",)
    obligations = _space().obligations
    for key in ("rule|U9|approve-list-read-only", "rule|U9|approve-pending-read-only"):
        assert obligations[key].strength == "e2e-evidence", key
        assert _credited_with(key) == ["*"], key


def test_compile_keeps_the_validation_and_non_mutation_cells() -> None:
    assert (
        _status("pair", OP="compile", PRIM="mcp").maps_to == "rule|U10|compile-validate-reads-mcp"
    )
    for prim in ("hook", "lsp"):
        assert _status("pair", OP="compile", PRIM=prim).maps_to == (
            "rule|U10|compile-render-only-non-mutation"
        )
    obligations = _space().obligations
    assert {
        "rule|U10|compile-validate-reads-mcp",
        "rule|U10|compile-render-only-non-mutation",
    } <= set(obligations)
    assert _credited_with("rule|U10|compile-render-only-non-mutation")


def test_prim_includes_bin_and_canvas() -> None:
    assert {"bin", "canvas"} <= set(FACTORS["PRIM"])


# -- E3: derived target parameters ----------------------------------------------


def test_compile_staged_targets_are_derived_from_the_capability_table() -> None:
    derived = set(compile_staged_targets())
    for name, profile in KNOWN_TARGETS.items():
        assert (name in derived) == ("instructions" not in profile.primitives), name
    assert derived and derived != set(KNOWN_TARGETS)
    assert _document()["parameters"]["compile_staged_targets"] == sorted(derived)


# -- evidence and output -----------------------------------------------------------


def _anchors():
    yield from (item for _, evidence in RULES.values() for item in evidence)
    yield from (item for evidence in ACCEPTS_EVIDENCE.values() for item in evidence)
    yield from REFUSAL_EVIDENCE.values()
    yield from USER_SCOPE_EVIDENCE.values()


def test_every_rule_cites_code_that_still_says_it() -> None:
    for anchor in _anchors():
        lines = (_ROOT / anchor.path).read_text(encoding="utf-8").splitlines()
        window = lines[anchor.line - 1 : anchor.line - 1 + EVIDENCE_WINDOW]
        assert any(anchor.needle in line for line in window), f"{anchor}: {anchor.needle!r}"


def test_generated_outputs_are_checked_in_and_hashed() -> None:
    text, rows = _generated()
    assert emit.drift() == []
    assert emit.generate() == (text, rows)
    document = json.loads(text)
    stored = document.pop("sha256")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert stored == hashlib.sha256(canonical.encode("ascii")).hexdigest()
    assert list(document["obligations"]) == sorted(document["obligations"])
    assert text.isascii() and all(row.isascii() for row in rows.values())


def test_rows_name_catalog_promises_and_witness_their_required_cells() -> None:
    catalog = promise_ids()
    obligations = _document()["obligations"]
    for name, text in _generated()[1].items():
        row = yaml.safe_load(text)
        assert set(row["promises"]) <= catalog, name
        witness = emit.WITNESS.format(row["id"])
        for cell in (
            c for step in _steps(row) if isinstance(step, dict) for c in step.get("cells", ())
        ):
            assert obligations[cell]["status"] == "required", cell
            assert obligations[cell]["witness"] == witness, cell
    for key, entry in obligations.items():
        assert entry["status"] in ("required", "baseline", "known-failing"), key
        assert entry["issue"] == emit.LANES[entry["lane"]], key
        assert ("witness" in entry) == (entry["status"] != "baseline"), key
        assert ("tracking" in entry) == (entry["status"] == "known-failing"), key


# -- negative control: only the oracle credits an obligation ------------------------


@pytest.fixture
def isolated_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receipts, "_EARNED", [])
    monkeypatch.delenv(receipts.DIRECTORY_ENV, raising=False)


def _observation(root: Path) -> oracles.Observation:
    state = LifecycleStateSnapshot.capture(root)
    artifacts = ArtifactSnapshotSet.capture({"project": root})
    return oracles.Observation(state, artifacts, frozenset(), root, None)


def _refusal_receipts(tmp_path: Path, key: str, *, touch: bool = False) -> list[receipts.Receipt]:
    (tmp_path / "apm.yml").write_text("name: p\n", encoding="ascii")
    before = _observation(tmp_path)
    if touch:
        (tmp_path / "apm.yml").write_text("name: q\n", encoding="ascii")
    after = _observation(tmp_path)
    result = CommandResult(
        ("apm", "install", "--frozen", "--update"), 2, "", "Error: x\n", tmp_path
    )
    replay.credit(key, before, after, result, ("*",))
    return list(receipts.earned())


_REFUSAL = "refusal|OP=install|FROZEN=on|UPDATE=on"


@pytest.mark.usefixtures("isolated_receipts")
def test_the_oracle_credits_a_generated_obligation(tmp_path: Path) -> None:
    obligation = Obligation(_REFUSAL, strength="e2e-evidence")
    assert uncovered([obligation], _refusal_receipts(tmp_path, _REFUSAL)) == []


@pytest.mark.usefixtures("isolated_receipts")
def test_without_the_oracle_call_the_obligation_stays_uncovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oracles, "check_obligation", lambda *args, **kwargs: None)
    obligation = Obligation(_REFUSAL, strength="e2e-evidence")
    assert uncovered([obligation], _refusal_receipts(tmp_path, _REFUSAL)) == [_REFUSAL]


@pytest.mark.usefixtures("isolated_receipts")
def test_a_refusal_that_wrote_bytes_earns_nothing(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match=r"project:apm\.yml changed"):
        _refusal_receipts(tmp_path, _REFUSAL, touch=True)
    assert receipts.earned() == ()


# -- machine witnesses -------------------------------------------------------------


def test_machine_witnessed_obligations_are_implied_by_their_credited_ids() -> None:
    cells = {cell.id: cell for cell in machine_cells()}
    witnessed = {
        key: entry for key, entry in _document()["obligations"].items() if "credited_by" in entry
    }
    assert witnessed
    for key, entry in witnessed.items():
        for machine_id in entry["credited_by"]:
            assert cells[machine_id].witness == entry["witness"], (key, machine_id)
            assert key in set(implied(cells[machine_id].assignment)), (key, machine_id)


@pytest.mark.usefixtures("isolated_receipts")
def test_a_machine_receipt_discharges_only_the_cells_it_implies(tmp_path: Path) -> None:
    key = "T4|OP=install|SCOPE=project|LOCK=newer-generator"
    entry = _document()["obligations"][key]
    assert "T4|install|project|newer" in entry["credited_by"]
    before = after = _observation(tmp_path)
    result = CommandResult(("apm", "install"), 1, "", "", tmp_path)
    oracles.check_obligation(
        "T4|install|project|newer",
        strength="e2e-evidence",
        before=before,
        after=after,
        result=result,
        evidence=oracles.Evidence(unchanged={"project": ("*",)}, exit=1),
    )
    earned = receipts.earned()
    aliased = Obligation(key, strength=entry["strength"], aliases=tuple(entry["credited_by"]))
    assert uncovered([aliased], earned) == []
    assert uncovered([Obligation(key, strength=entry["strength"])], earned) == [key]
    other = Obligation("T4|OP=install|SCOPE=user|LOCK=newer-generator", strength="e2e")
    assert uncovered([other], earned) == [other.id]


# -- helpers --------------------------------------------------------------------


def _parse(key: str) -> tuple[str, dict[str, str]]:
    family, *parts = key.split("|")
    return family, dict(part.split("=", 1) for part in parts)


def _steps(row: dict) -> list[object]:
    return [*row.get("given", ()), *row.get("when", ())]


def _credited_with(key: str) -> list[str]:
    """The ``unchanged`` evidence of the row step that credits *key*."""
    for text in _generated()[1].values():
        for step in _steps(yaml.safe_load(text)):
            if isinstance(step, dict) and key in step.get("cells", ()):
                return list(step.get("unchanged", ()))
    return []


# -- definition-rule removals, restated from the rule text (steward T-5 audit) --
#
# Each removal rule is restated here from its text and its source evidence,
# independently of rules.definition_rule, and checked against the committed
# obligations file both ways: every removal satisfies the rule, and every raw
# candidate that satisfies it is removed (U6 is checked before U8).

_OBLIGATIONS = Path(__file__).parents[3] / "tests" / "quality" / "lifecycle_obligations.json"
_MARKETPLACE_ADD_SOURCES = frozenset({"marketplace-git", "marketplace-local", "marketplace-file"})
_MCP_SERVER_SOURCES = frozenset({"mcp-registry", "inline-mcp"})


def _u6(cell) -> bool:
    """pack provenance is the dependencies / marketplace block, never a SRC (pack.py:32-33)."""
    return cell.get("OP") == "pack" and cell.get("SRC") is not None


def _u8(cell) -> str | None:
    """The U8 removal clause of a cell: a source the CLI grammar cannot state for its command."""
    op, src, prim = cell.get("OP"), cell.get("SRC"), cell.get("PRIM")
    if src is None or _u6(cell):
        return None
    if op == "bundle-install" and src != "local-bundle":
        return "bundle-install from a non-bundle source"  # install.py:1233 detect_local_bundle
    if op == "mcp-install" and src not in _MCP_SERVER_SOURCES:
        return "install --mcp from a package source"  # install.py:971 --url, 997 --registry
    if src in _MCP_SERVER_SOURCES and prim not in (None, "mcp"):
        return "an MCP server source for a non-MCP primitive"
    return None


def _u8_mapping(cell) -> str | None:
    """The U8 mapping clause of a cell: same command as another cell, or parsed then refused."""
    op, src = cell.get("OP"), cell.get("SRC")
    # A modifier the command does not accept is a U1 usage refusal first.
    if src is None or _u8(cell) is not None or _u6(cell) or refusal_cell(cell) is not None:
        return None
    if op == "install" and src == "local-bundle":
        return "install of a local bundle is bundle-install"  # install.md: apm install ./b.zip
    if op == "marketplace" and src not in _MARKETPLACE_ADD_SOURCES:
        return "marketplace add of a source without a catalog"  # registration.py:45
    if op == "uninstall" and src in _MCP_SERVER_SOURCES:
        return f"uninstall of an MCP server ({src})"  # uninstall/engine.py:698, 857
    return None


def _u3(cell) -> bool:
    """edited is relative to a receipt; unknown provenance has no lock (deployment_ledger.py:588)."""
    deployed, lock = cell.get("DEPLOYED"), cell.get("LOCK")
    return (deployed in ("clean", "edited") and lock == "absent") or (
        deployed == "present-provenance-unknown" and lock not in (None, "absent")
    )


@cache
def _committed_removed() -> dict[str, tuple[str, ...]]:
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    return {key: tuple(rules) for key, rules in data["removed"].items()}


def _removed_by(rule: str) -> set[str]:
    return {key for key, rules in _committed_removed().items() if rule in rules}


@cache
def _raw() -> dict[str, dict[str, str]]:
    return {cell_id(family, cell): cell for family, cell in raw_cells()}


@pytest.mark.parametrize(
    ("rule", "holds"),
    [("U3", _u3), ("U6", _u6), ("U8", lambda cell: _u8(cell) is not None)],
)
def test_definition_rule_removes_exactly_the_cells_its_text_names(rule, holds) -> None:
    removed = _removed_by(rule)
    named = {key for key, cell in _raw().items() if holds(cell)}
    assert removed == named, {
        "removed but not named": sorted(removed - named)[:10],
        "named but not removed": sorted(named - removed)[:10],
    }


def test_u8_removals_split_by_the_source_each_command_takes() -> None:
    clauses: dict[str, int] = {}
    for key in _removed_by("U8"):
        clause = _u8(_raw()[key])
        clauses[clause] = clauses.get(clause, 0) + 1
    assert clauses == {
        "bundle-install from a non-bundle source": 81,
        "install --mcp from a package source": 72,
        "an MCP server source for a non-MCP primitive": 132,
    }
    assert sum(clauses.values()) == 285


def test_u8_mappings_name_the_cell_or_refusal_each_shape_lands_on() -> None:
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    mapped = {key: entry for key, entry in data["mapped"].items() if entry["rules"][0] == "U8"}
    named = {key for key, cell in _raw().items() if _u8_mapping(cell) is not None}
    assert set(mapped) == named, sorted(set(mapped) ^ named)[:10]
    clauses: dict[str, int] = {}
    for key, entry in mapped.items():
        family, cell = parse_cell(key)
        clause = _u8_mapping(cell)
        clauses[clause] = clauses.get(clause, 0) + 1
        if cell["OP"] == "install":
            # The bundle-install cell itself, or the refusal its own modifier lands on.
            target = cell_id(family, {**cell, "OP": "bundle-install"})
            assert entry["maps_to"] in (target, data["mapped"].get(target, {}).get("maps_to")), key
        elif cell["OP"] == "marketplace":
            assert entry["maps_to"] == MARKETPLACE_ADD_REFUSAL, key
        else:
            assert entry["maps_to"] == UNINSTALL_MCP_REFUSALS[cell["SRC"]], key
    assert clauses == {
        "install of a local bundle is bundle-install": 9,
        "marketplace add of a source without a catalog": 44,
        "uninstall of an MCP server (mcp-registry)": 3,
        "uninstall of an MCP server (inline-mcp)": 3,
    }


def test_every_removed_and_mapped_cell_names_its_rules() -> None:
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    for key, rules in data["removed"].items():
        assert rules and set(rules) <= set(RULES), key
    for key, entry in data["mapped"].items():
        assert entry["rules"] and set(entry["rules"]) <= set(RULES), key
        assert entry["maps_to"] in data["obligations"] or entry["maps_to"].startswith(
            ("refusal|", "rule|")
        ), key


# -- U-BIN: closed-world pin of the bin/ deploy domain ------------------------------


@dataclass(frozen=True)
class MutationCase:
    """A source edit that widens one pinned element of U-BIN."""

    name: str
    path: str
    old: str
    new: str


_BIN_MUTATIONS = sorted(
    (
        MutationCase(
            "plugin-bin-deployable-drops-the-scope-check",
            "install/exec_gate.py",
            "        or scope is not InstallScope.USER\n",
            "",
        ),
        MutationCase(
            "second-bin-writer",
            "install/deployment_plan.py",
            "\n\ndef ",
            "\n\ndef _second_bin_writer(integrator, *args):\n"
            "    return integrator._deploy_bin_files(*args)\n\n\ndef ",
        ),
        MutationCase(
            "unpack-accepts-a-bin-executable",
            "bundle/unpacker.py",
            '_EXECUTABLE_DIR_NAMES = frozenset({"hooks", "bin"})',
            '_EXECUTABLE_DIR_NAMES = frozenset({"hooks"})',
        ),
    ),
    key=lambda case: case.name,
)


def _source_copy(root: Path) -> Path:
    import shutil

    target = root / "apm_cli"
    shutil.copytree(bin_pin.SRC_ROOT, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def test_u_bin_pin_matches_the_source_and_excludes_exactly_its_cells() -> None:
    assert bin_pin.pin_digest() == U_BIN_PIN
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    assert data["pins"] == {"U-BIN": U_BIN_PIN}
    assert data["excluded"] == {key: ["U-BIN"] for key in U_BIN_CELLS}
    assert len(U_BIN_CELLS) == 41 and not data["reinstated"]


@pytest.mark.parametrize("case", _BIN_MUTATIONS, ids=lambda case: case.name)
def test_widening_bin_deploy_reinstates_the_u_bin_cells(tmp_path: Path, case) -> None:
    source = _source_copy(tmp_path)
    assert bin_pin.pin_digest(source) == U_BIN_PIN  # control: an unchanged copy keeps the pin
    path = source / case.path
    text = path.read_text(encoding="utf-8")
    assert text.count(case.old) >= 1, case
    path.write_text(text.replace(case.old, case.new, 1), encoding="utf-8")
    digest = bin_pin.pin_digest(source)
    assert digest != U_BIN_PIN, case.name
    excluded, reinstated = emit.u_bin_audit(digest)
    assert not excluded
    assert set(reinstated) == set(U_BIN_CELLS)
    assert all(entry == {"rules": ["U-BIN"], "must_cover": True} for entry in reinstated.values())
