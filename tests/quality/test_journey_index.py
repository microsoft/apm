"""The stable journey catalogue: tests/lifecycle/journeys/index.yaml (refs #4110).

The index names all 25 consumer, producer and enterprise journeys, their
steps and the output oracles (O1-O13) and normalizers (N1-N7) they use. The
release-acceptance skill and the journey runner both read it, so its ids,
step numbers and oracle ids are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.component, pytest.mark.promise("P-DOC-journey-index")]

RATCHET_TEST_SCOPE = "repository"

_ROOT = Path(__file__).parents[2]
INDEX = _ROOT / "tests" / "lifecycle" / "journeys" / "index.yaml"
DOCS = _ROOT / "docs" / "src" / "content" / "docs"
PERSONA_DIRS = ("consumer", "producer", "enterprise")
_ID = re.compile(r"^(J0[1-9]|J1[0-5]|P0[1-4]|E0[1-6])$")
_ORACLE_ID = re.compile(r"^O([1-9]|1[0-3])$")
_NORMALIZER_ID = re.compile(r"^N[1-7]$")
GOLDEN = frozenset({"J01", "J02", "J03", "E01"})
KINDS = frozenset({"apm", "shell", "edit", "manual"})
_JOURNEY_KEYS = {"id", "persona", "title", "golden", "tier", "docs", "fixtures", "steps"}
_STEP_KEYS = {"n", "cmd", "kind", "exit", "key_lines", "forbidden_lines", "files", "oracles"}


def _load(path: Path = INDEX) -> dict:
    return yaml.safe_load(path.read_text(encoding="ascii"))


def check_index(index: dict, docs_root: Path = DOCS) -> list[str]:
    """Every schema problem of *index* (empty when it is well formed)."""
    problems: list[str] = []
    if index.get("schema_version") != 1:
        problems.append(f"schema_version must be 1, got {index.get('schema_version')!r}")
    oracles = index.get("oracles") or {}
    normalizers = index.get("normalizers") or {}
    if set(oracles) != {f"O{i}" for i in range(1, 14)}:
        problems.append(f"oracles must be exactly O1-O13, got {sorted(oracles)}")
    for oracle_id, oracle in oracles.items():
        missing = {"name", "invariant", "check", "applies"} - set(oracle or {})
        if missing:
            problems.append(f"oracle {oracle_id} lacks {sorted(missing)}")
    if set(normalizers) != {f"N{i}" for i in range(1, 8)}:
        problems.append(f"normalizers must be exactly N1-N7, got {sorted(normalizers)}")
    journeys = index.get("journeys") or []
    ids = [journey.get("id") for journey in journeys]
    if len(ids) != 25 or len(set(ids)) != 25:
        problems.append(f"expected 25 unique journey ids, got {len(set(ids))} of {len(ids)}")
    problems.extend(f"bad journey id {jid!r}" for jid in ids if not _ID.match(str(jid)))
    golden = {journey.get("id") for journey in journeys if journey.get("golden") is True}
    if golden != GOLDEN:
        problems.append(f"golden journeys must be {sorted(GOLDEN)}, got {sorted(golden)}")
    # A step may also name an invariant (I1-I9) that some oracle's invariant field covers.
    defined = set(oracles) | {
        part.strip()
        for oracle in oracles.values()
        for part in str((oracle or {}).get("invariant", "")).split(",")
        if re.fullmatch(r"I[1-9]", part.strip())
    }
    for journey in journeys:
        problems.extend(_check_journey(journey, defined, docs_root))
    return problems


def _check_journey(journey: dict, oracles: set[str], docs_root: Path) -> list[str]:
    jid = journey.get("id")
    problems = [f"{jid}: missing {key}" for key in sorted(_JOURNEY_KEYS - set(journey))]
    if not isinstance(journey.get("golden"), bool):
        problems.append(f"{jid}: golden must be a boolean")
    for doc in journey.get("docs") or []:
        if not (docs_root / doc).is_file():
            problems.append(f"{jid}: docs page not found: {doc}")
    unresolved = journey.get("docs_unresolved") or []
    if bool(journey.get("docs_missing")) != bool(unresolved):
        problems.append(f"{jid}: docs_missing must be true exactly when docs_unresolved is set")
    problems.extend(
        f"{jid}: docs_unresolved page now exists, move it to docs: {doc}"
        for doc in unresolved
        if (docs_root / doc).is_file()
    )
    steps = journey.get("steps") or []
    numbers = [step.get("n") for step in steps]
    if numbers != list(range(1, len(steps) + 1)) or not steps:
        problems.append(f"{jid}: steps must number 1..{len(steps)}, got {numbers}")
    for step in steps:
        where = f"{jid} step {step.get('n')}"
        problems.extend(f"{where}: missing {key}" for key in sorted(_STEP_KEYS - set(step)))
        if step.get("kind") not in KINDS:
            problems.append(f"{where}: kind {step.get('kind')!r} not in {sorted(KINDS)}")
        if not isinstance(step.get("exit"), (int, str)) or isinstance(step.get("exit"), bool):
            problems.append(f"{where}: exit must be an int or a conditional string")
        argv = step.get("argv")
        if argv is not None and not (
            step.get("kind") == "apm"
            and isinstance(argv, list)
            and argv
            and all(
                isinstance(a, list) and a and a[0] == "apm" and all(isinstance(t, str) for t in a)
                for a in argv
            )
        ):
            problems.append(f"{where}: argv must be a list of apm argv lists on an apm step")
        unknown = set(step.get("oracles") or ()) - set(oracles)
        if unknown:
            problems.append(f"{where}: undefined oracles {sorted(unknown)}")
    return problems


def test_index_is_ascii() -> None:
    INDEX.read_bytes().decode("ascii")


def test_index_matches_the_pinned_schema() -> None:
    assert check_index(_load()) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda idx: idx["journeys"][0].update(id="J16"), "bad journey id"),
        (lambda idx: idx["journeys"][1].update(id=idx["journeys"][0]["id"]), "25 unique"),
        (lambda idx: idx["journeys"][0]["steps"].pop(0), "steps must number"),
        (lambda idx: idx["journeys"][0]["steps"][0].update(oracles=["O99"]), "undefined"),
        (lambda idx: idx["oracles"].pop("O13"), "exactly O1-O13"),
        (lambda idx: idx.update(schema_version=2), "schema_version"),
        (lambda idx: idx["journeys"][0]["docs"].append("nope/missing.md"), "not found"),
        (lambda idx: idx["journeys"][0]["steps"][0].update(kind="robot"), "kind"),
        (lambda idx: idx["journeys"][0]["steps"][0].update(argv=[["apm", 42]]), "argv"),
        (lambda idx: idx["journeys"][0]["steps"][0].update(argv="apm --version"), "argv"),
    ],
)
def test_a_mutated_index_fails(mutation, expected: str) -> None:
    index = _load()
    mutation(index)
    assert any(expected in problem for problem in check_index(index))


def test_every_docs_path_exists_on_disk() -> None:
    """Each journey's docs list names only pages under docs/src/content/docs/ (.md or .mdx)."""
    missing = [
        (journey["id"], doc)
        for journey in _load()["journeys"]
        for doc in journey["docs"]
        if not (DOCS / doc).is_file()
    ]
    assert missing == [], f"docs pages not on disk (move them to docs_unresolved): {missing}"


def test_golden_mutation_fails() -> None:
    index = _load()
    for journey in index["journeys"]:
        journey["golden"] = journey["id"] in {"J01", "J02"}
    assert any("golden journeys" in problem for problem in check_index(index))


def _persona_journeys() -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for persona in PERSONA_DIRS:
        for path in sorted((INDEX.parent / persona).glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="ascii"))
            found.setdefault(persona, {})[data["id"]] = len(data.get("steps") or [])
    return found


def test_persona_journey_files_agree_with_the_index() -> None:
    """Each persona with journey files carries exactly the index's ids and step counts.

    Personas roll out one directory at a time (#4110), so a persona with no files yet is
    not required; once a persona has any file, all of its journeys must be present.
    """
    found = _persona_journeys()
    if not found:
        pytest.skip("no tests/lifecycle/journeys/{consumer,producer,enterprise}/*.yaml yet (#4110)")
    for persona, journeys in found.items():
        expected = {
            journey["id"]: len(journey["steps"])
            for journey in _load()["journeys"]
            if journey["persona"] == persona
        }
        assert journeys == expected, persona
