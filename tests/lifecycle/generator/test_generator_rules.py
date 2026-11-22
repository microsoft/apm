"""Rules, determinism and receipts of the lifecycle obligation generator (#4099).

Pins the steward rules (U1-U10, E1-E3) on the generated set, the evidence
anchors of every rule, the derived target parameters, the checked-in output
(no drift, no aggregate, merge-friendly) and the negative control: an obligation is credited
only by ``oracles.check_obligation``; without that call it stays uncovered.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import subprocess
from functools import cache
from pathlib import Path

import pytest
import yaml

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.lifecycle import oracles, receipts
from tests.lifecycle.generator import anchors, bin_pin, emit, replay
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
    FIRST_PARTY_MCP_CELL,
    LOCAL_BUNDLE_REFUSALS,
    MARKETPLACE_ADD_REFUSAL,
    REFUSAL_EVIDENCE,
    RETIRED_MCP_OWNERS,
    RULE_CELL_REFUSALS,
    RULES,
    U_BIN_PIN,
    UNINSTALL_MCP_REFUSALS,
    USER_SCOPE_EVIDENCE,
    Verdict,
    classify,
    refusal_cell,
)
from tests.lifecycle.generator.space import Entry, build_space, raw_cells
from tests.lifecycle.generator.witnesses import credits, machine_cells
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


def _line_citations() -> list[str]:
    sources = sorted(Path(anchors.__file__).parent.glob("*.py"))
    found = [
        f"{path.name}:{number}: {match.group(0)}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in anchors.LINE_CITATION.finditer(line)
    ]
    return found + [
        f"{rule}: {item}"
        for rule, entry in _document()["rules"].items()
        for item in entry["evidence"]
        if anchors.LINE_CITATION.search(item)
    ]


def test_every_rule_cites_a_symbol_that_still_says_it() -> None:
    """Evidence names a symbol, never a line number (#4696): a line citation moves
    with every edit to the cited file, so unrelated PRs conflict in a merge train."""
    for anchor in _anchors():
        spans = anchors.symbols(anchor.path)
        assert anchor.symbol in spans, f"{anchor}: symbol is gone"
        start, end = spans[anchor.symbol]
        lines = (_ROOT / anchor.path).read_text(encoding="utf-8").splitlines()
        body = "\n".join(lines[start - 1 : end])
        assert anchor.needle in body, f"{anchor}: {anchor.needle!r}"
    assert _line_citations() == []
    assert anchors.LINE_CITATION.search("see install/services.py" + ":1025")
    assert anchors.LINE_CITATION.search("docs/a.md" + ":4-9")
    assert not anchors.LINE_CITATION.search("src/apm_cli/commands/install.py::install")


def test_a_markdown_section_runs_past_comments_in_its_code_fences(tmp_path: Path) -> None:
    """A shell comment inside a fence is code, not a heading: it neither ends the
    section nor becomes an anchor of its own. A fence line with an info string
    inside a block is content, not its close."""
    page = tmp_path / "page.md"
    page.write_text(
        "# Page\n\n## Add a server\n\n```bash\n# From a registry\napm install --mcp x\n"
        "```not-a-closing-fence\n## Code, not a heading\n"
        "````\n\n~~~\n## Not a heading\n~~~\n\nRe-declare it.\n\n## Next\n",
        encoding="utf-8",
    )
    spans = anchors.symbols("page.md", tmp_path)
    assert sorted(spans) == ["", "Add a server", "Next", "Page"]
    start, end = spans["Add a server"]
    section = page.read_text(encoding="utf-8").splitlines()[start - 1 : end]
    assert "Re-declare it." in section
    assert "## Next" not in section


def test_generated_outputs_are_checked_in_without_aggregates() -> None:
    text, rows = _generated()
    assert emit.drift() == []
    assert emit.generate() == (text, rows)
    document = json.loads(text)
    assert not {"counts", "sha256"} & set(document), "aggregates make every PR conflict"
    assert emit.render(document) == text
    assert list(document["obligations"]) == sorted(document["obligations"])
    assert text.isascii() and all(row.isascii() for row in rows.values())
    assert emit.counts()["obligations_total"] == len(document["obligations"])


def _promote(document: dict, key: str, node: str) -> None:
    """Credit *key* the way a machine witness does (status, lane, witness, credited_by)."""
    record = document["obligations"][key]
    document["obligations"][key] = {
        **record,
        "credited_by": [f"X|{node}"],
        "issue": "#4096",
        "lane": "T-2",
        "status": "required",
        "witness": f"tests/lifecycle/test_{node}.py::test_{node}",
    }


def _merge(tmp_path: Path, base: dict, ours: dict, theirs: dict) -> subprocess.CompletedProcess:
    paths = []
    for name, document in (("ours", ours), ("base", base), ("theirs", theirs)):
        path = tmp_path / f"{name}.json"
        path.write_text(emit.render(document), encoding="ascii")
        paths.append(str(path))
    return subprocess.run(
        ["git", "merge-file", "-p", *paths], capture_output=True, text=True, check=False
    )


def test_parallel_prs_crediting_adjacent_cells_merge_cleanly(tmp_path: Path) -> None:
    """Two PRs that credit neighbouring cells merge to exactly their regenerated union."""
    base = json.loads(_generated()[0])
    keys = list(base["obligations"])
    first = next(
        key
        for key, after in itertools.pairwise(keys)
        if base["obligations"][key]["status"] == base["obligations"][after]["status"] == "baseline"
    )
    second = keys[keys.index(first) + 1]
    ours, theirs, union = (copy.deepcopy(base) for _ in range(3))
    _promote(ours, first, "ours")
    _promote(theirs, second, "theirs")
    _promote(union, first, "ours")
    _promote(union, second, "theirs")
    merged = _merge(tmp_path, base, ours, theirs)
    assert merged.returncode == 0, merged.stdout
    assert merged.stdout == emit.render(union)
    # Negative control: a content hash (the retired ``sha256`` key) differs on every
    # side, so it brings the conflict back.
    for document in (base, ours, theirs):
        document["sha256"] = hashlib.sha256(emit.render(document).encode("ascii")).hexdigest()
    assert _merge(tmp_path, base, ours, theirs).returncode != 0


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
    cells: dict[str, list] = {}
    for cell in machine_cells():
        cells.setdefault(cell.id, []).append(cell)
    witnessed = {
        key: entry for key, entry in _document()["obligations"].items() if "credited_by" in entry
    }
    assert witnessed
    for key, entry in witnessed.items():
        for machine_id in entry["credited_by"]:
            assert {c.witness for c in cells[machine_id]} == {entry["witness"]}, (key, machine_id)
            assert any(key in set(credits(c)) for c in cells[machine_id]), (key, machine_id)


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
    """pack provenance is the dependencies / marketplace block, never a SRC (pack.py::_PACK_HELP)."""
    return cell.get("OP") == "pack" and cell.get("SRC") is not None


def _u8(cell) -> str | None:
    """The U8 removal clause of a cell: a source the CLI grammar cannot state for its command."""
    op, src, prim = cell.get("OP"), cell.get("SRC"), cell.get("PRIM")
    if src is None or _u6(cell):
        return None
    if op == "bundle-install" and src != "local-bundle":
        return "bundle-install from a non-bundle source"  # install.py::install detect_local_bundle
    if op == "mcp-install" and src not in _MCP_SERVER_SOURCES:
        return "install --mcp from a package source"  # install.py::install --url, --registry
    if src in _MCP_SERVER_SOURCES and prim not in (None, "mcp"):
        return "an MCP server source for a non-MCP primitive"
    if src == "native-plugin" and prim == "canvas":
        return (
            "a native plugin carries no canvas"  # canvas_integrator.py docstring: .apm/extensions/
        )
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
        return "marketplace add of a source without a catalog"  # registration.py::MarketplaceManifestNotFoundError
    if op == "uninstall" and src in _MCP_SERVER_SOURCES:
        return f"uninstall of an MCP server ({src})"  # uninstall/engine.py::_mcp_server_hint
    if src == "local-bundle" and op in ("uninstall", "update", "prune"):
        return f"{op} of a local bundle"  # _mcp_server_hint, update.py::_explain_local_bundles, prune.py::prune
    if src == "local-bundle" and op is None and {"FROZEN", "UPDATE"} & set(cell):
        # Only an install states a bundle; it refuses --frozen ("positional packages")
        # and --update (install.py rejected_flags), so the pair lands on that refusal.
        return "a local bundle under a modifier its install refuses"
    return None


def _u3(cell) -> bool:
    """edited is relative to a receipt; unknown provenance has no lock (deployment_ledger.py::DeploymentLedgerCodec)."""
    deployed, lock = cell.get("DEPLOYED"), cell.get("LOCK")
    return (deployed in ("clean", "edited") and lock == "absent") or (
        deployed == "present-provenance-unknown" and lock not in (None, "absent")
    )


def _u_mcpown_user(cell) -> bool:
    """Retired MCP owners exist only at project scope (retired_destinations.py::split_mcp_claims)."""
    return cell.get("SCOPE") == "user" and cell.get("MCPOWN") in ("vscode", "legacy-receipt")


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
    [
        ("U3", _u3),
        ("U6", _u6),
        ("U8", lambda cell: _u8(cell) is not None),
        ("U-BIN", lambda cell: _u_bin(cell)),
        ("U-BIN-OPS", lambda cell: _u_bin_ops(cell)),
        ("U-MCPOWN-USER", _u_mcpown_user),
    ],
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
        "a native plugin carries no canvas": 10,
    }
    assert sum(clauses.values()) == 295


def test_executable_gate_levels_of_an_mcp_source_map_to_the_first_party_rule_cell() -> None:
    """U-FIRST-PARTY-MCP: the project's own MCP servers are never gated by executables.allow.

    Every executable-gate level of an MCP source lands on one must-cover rule cell
    that the M-SOURCE spine witnesses; the org-policy deny and GATE=none stay cells.
    """
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    mapped = {
        key for key, entry in data["mapped"].items() if entry["rules"] == ["U-FIRST-PARTY-MCP"]
    }
    executable = [
        name for name, authority in GATE_AUTHORITY.items() if authority == "executable-gate"
    ]
    assert mapped == {
        cell_id("pair", {"SRC": src, "GATE": gate})
        for src in ("inline-mcp", "mcp-registry")
        for gate in executable
    } | {
        cell_id("T1", {"OP": "mcp-install", "PRIM": "mcp", "GATE": gate}) for gate in executable
    } | {
        # R5: approve / deny decide only installed packages (see the live scan test).
        cell
        for src in ("inline-mcp", "mcp-registry")
        for cell in (
            cell_id("pair", {"OP": "approve-deny", "SRC": src}),
            cell_id("T3", {"OP": "approve-deny", "SRC": src, "PRIM": "mcp"}),
        )
    }
    assert all(data["mapped"][key]["maps_to"] == FIRST_PARTY_MCP_CELL for key in mapped)
    rule = data["obligations"][FIRST_PARTY_MCP_CELL]
    assert rule["must_cover"] and rule["status"] == "required", rule
    assert rule["witness"].endswith("test_source_spine_covers_every_form_and_op"), rule
    for src in ("inline-mcp", "mcp-registry"):
        for gate in ("none", "denied-dependency"):
            assert cell_id("pair", {"SRC": src, "GATE": gate}) in data["obligations"]


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
        if "OP" not in cell:
            modifier = next(name for name in ("FROZEN", "UPDATE") if name in cell)
            refusal = cell_id("refusal", {"OP": "bundle-install", modifier: "on"})
            assert entry["maps_to"] == refusal, key
        elif cell["OP"] == "install":
            # The bundle-install cell itself, or the refusal its own modifier lands on.
            target = cell_id(family, {**cell, "OP": "bundle-install"})
            assert entry["maps_to"] in (target, data["mapped"].get(target, {}).get("maps_to")), key
        elif cell["OP"] == "marketplace":
            assert entry["maps_to"] == MARKETPLACE_ADD_REFUSAL, key
        elif cell["SRC"] == "local-bundle":
            assert entry["maps_to"] == LOCAL_BUNDLE_REFUSALS[cell["OP"]], key
        else:
            assert entry["maps_to"] == UNINSTALL_MCP_REFUSALS[cell["SRC"]], key
    assert clauses == {
        "install of a local bundle is bundle-install": 9,
        "marketplace add of a source without a catalog": 43,
        "uninstall of a local bundle": 9,
        "update of a local bundle": 9,
        "prune of a local bundle": 9,
        "uninstall of an MCP server (mcp-registry)": 3,
        "uninstall of an MCP server (inline-mcp)": 3,
        "a local bundle under a modifier its install refuses": 2,
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


# -- U-BIN: bin/ at project scope is unreachable (self-reinstating) -------------


def _u_bin(cell) -> bool:
    """bin/ deploys only at user scope (exec_gate.py::plugin_bin_deployable: scope is not InstallScope.USER)."""
    return cell.get("PRIM") == "bin" and cell.get("SCOPE") == "project"


def _u_bin_ops(cell) -> bool:
    """A gate level of bin/ under a command bin/ never reaches; an org deny stays a cell."""
    return (
        set(cell) == {"OP", "PRIM", "GATE"}
        and cell["PRIM"] == "bin"
        and cell["OP"] in ("audit", "prune", "pack", "marketplace", "bundle-install")
        and cell["GATE"] != "denied-dependency"
    )


def test_u_bin_ops_premises_hold_in_the_live_cli() -> None:
    """Tripwire: U-BIN-OPS lapses the moment a premise changes in the source.

    audit, prune, pack and every marketplace subcommand must keep no user-scope
    form, and plugin_bin_deployable must keep requiring a marketplace plugin at
    user scope (the U-BIN pin hashes that predicate); the closed-world pin of the
    bin/ writer (security/u_bin.py) keeps a local bundle from ever writing bin/.
    """
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from tests.lifecycle.security import u_bin

    subcommands = ("package", "add", "list", "browse", "update", "remove", "audit", "check")
    commands = [("audit",), ("prune",), ("pack",)]
    commands += [("marketplace", name) for name in subcommands]
    for words in commands:
        result = CliRunner().invoke(cli, [*words, "--help"])
        assert result.exit_code == 0, (words, result.output)
        assert "--global" not in result.output and "-g," not in result.output, words
    assert bin_pin.predicate_digest() == U_BIN_PIN
    assert u_bin.pin_holds()


def test_u_bin_pin_matches_the_predicate_and_is_recorded() -> None:
    assert bin_pin.predicate_digest() == U_BIN_PIN
    data = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))
    assert data["pins"] == {"U-BIN": U_BIN_PIN}


def test_a_changed_predicate_reinstates_the_u_bin_cells_as_must_cover(monkeypatch) -> None:
    cell = {"PRIM": "bin", "SCOPE": "project"}
    assert classify("pair", cell) == Verdict("removed", ("U-BIN",))
    monkeypatch.setattr(bin_pin, "predicate_digest", lambda *_: "0" * 64)
    assert classify("pair", cell) == Verdict("obligation", ("U-BIN",), must_cover=True)


def test_a_strict_only_spine_never_hides_a_passing_spine() -> None:
    """The user-scope bin spine implies this pair only through strict #3703 cells and
    sorts first by node id; the project matrix passes it, so it stays required."""
    key = "pair|OP=install|GATE=denied-dependency"
    entry = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))["obligations"][key]
    assert entry["status"] == "required", entry
    assert entry["witness"].endswith("test_gate_matrix_credits_op_prim_gate_cells[project]"), entry
    assert "tracking" not in entry, entry


# -- U-MCPOWN-USER: bound to the live retired-claim reader ---------------------------


def test_u_mcpown_user_holds_only_while_the_reader_ignores_user_scope(tmp_path) -> None:
    """The removal stands only while split_mcp_claims returns no retired claims at user scope.

    A pre-0.37 lock with a vscode claim and an unsettled copilot claim yields
    retired claims at project scope; the same lock at user scope yields none.
    If the reader ever reports user-scope retired claims, the U-MCPOWN-USER
    cells are reachable again and this test fails.
    """
    from apm_cli.install.mcp.retired_destinations import split_mcp_claims

    def claims(user_scope: bool):
        servers = {"copilot": ["srv"], "vscode": ["srv"]}
        hashes = {"copilot": {"srv": "sha256:" + "0" * 64}, "vscode": {"srv": "sha256:" + "1" * 64}}
        return split_mcp_claims(
            servers, hashes, apm_version="0.36.0", project_root=tmp_path, user_scope=user_scope
        )

    project = claims(user_scope=False)
    assert project.names["vscode"] == {"srv"}, "the fixture lock carries a retired claim"
    user = claims(user_scope=True)
    assert not any(user.names.values()) and not any(user.hashes.values()), user
    removed = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))["removed"]
    recorded = {key for key, rules in removed.items() if "U-MCPOWN-USER" in rules}
    assert recorded == {f"pair|SCOPE=user|MCPOWN={owner}" for owner in RETIRED_MCP_OWNERS}, recorded


def test_only_a_changed_target_set_routes_an_offline_pair_to_target_history() -> None:
    """An unchanged TGT is no target history: its offline pair belongs to T-6e."""
    for tgt, lane in (
        ("single-widened", "T-6a"),
        ("multi-narrowed", "T-6a"),
        ("changed", "T-6a"),
        ("single-unchanged", "T-6e"),
        ("multi-unchanged", "T-6e"),
    ):
        cell = {"TGT": tgt, "NET": "offline"}
        entry = Entry(id=cell_id("pair", cell), family="pair", cell=cell, strength="e2e")
        assert emit.lane_for(entry) == lane, (tgt, emit.lane_for(entry))
    ledger = json.loads(_OBLIGATIONS.read_text(encoding="utf-8"))["obligations"]
    unchanged = {
        key: entry["lane"]
        for key, entry in ledger.items()
        if entry["family"] == "pair" and "NET=offline" in key and "-unchanged" in key
    }
    assert len(unchanged) == 2 and set(unchanged.values()) == {"T-6e"}, unchanged
