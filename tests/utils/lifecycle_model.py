"""Pure Phase 1 intent model and version-independent transition replay inputs.

This module does not import product code, a deployment ledger, or Hypothesis.
The fixture is deliberately one pinned Git package, one skill, and Copilot in
project scope; it is not a model of all APM routing or transaction semantics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

TRANSITIONS = (
    "install",
    "reinstall",
    "dry_run",
    "tamper",
    "audit_tampered",
    "repair",
    "audit_clean",
    "remove_declaration",
    "readd_declaration",
    "prune_removed",
)
OPEN_WORLD = "filesystem.open_world_observation"
OWNERSHIP = "ownership.preserve_unowned"
ROUTING = "routing.authorized_targets_only"
SOURCE = "source.ref_cache_coherent"
OUTCOME = "outcome.status_matches_state"
IDEMPOTENCY = "idempotency.byte_stable"
FAILED_COMMAND = "transaction.failed_command_preserves_state"
READ_ONLY = "observation.read_only_preserves_state"
LAWS = frozenset(
    {OPEN_WORLD, OWNERSHIP, ROUTING, SOURCE, OUTCOME, IDEMPOTENCY, FAILED_COMMAND, READ_ONLY}
)
FIXTURE_ID = "copilot-project-single-skill-v1"
CORPUS_PATH = Path(__file__).parents[1] / "fixtures" / "lifecycle_transition_corpus.json"


@dataclass(frozen=True)
class LifecycleModel:
    """Expected intent and materialization, never inferred from product output."""

    declared: bool = True
    materialized: bool = False
    clean: bool = True

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.declared, self.materialized, self.clean)):
            raise ValueError("Model fields must be booleans")
        if not self.materialized and not self.clean:
            raise ValueError("Absent materialization cannot be tampered")

    @property
    def locked(self) -> bool:
        """This fixture acquires and removes its only lock with materialization."""
        return self.materialized


def legal_transitions(state: LifecycleModel) -> tuple[str, ...]:
    """Enumerate every enabled transition, including non-CLI fixture actions."""
    enabled = {
        "install": state.declared and not state.materialized,
        "reinstall": state.declared and state.materialized and state.clean,
        "dry_run": state.declared,
        "tamper": state.materialized and state.clean,
        "audit_tampered": state.declared and state.materialized and not state.clean,
        "repair": state.declared and state.materialized and not state.clean,
        "audit_clean": state.declared and state.materialized and state.clean,
        "remove_declaration": state.declared,
        "readd_declaration": not state.declared,
        "prune_removed": not state.declared and state.materialized,
    }
    return tuple(name for name in TRANSITIONS if enabled[name])


def advance(state: LifecycleModel, transition: str) -> LifecycleModel:
    """Apply a legal intent transition without observing or touching a filesystem."""
    if transition not in legal_transitions(state):
        raise ValueError(f"Illegal transition {transition!r} from {state!r}")
    if transition in {"install", "repair"}:
        return replace(state, materialized=True, clean=True)
    if transition == "tamper":
        return replace(state, clean=False)
    if transition in {"remove_declaration", "readd_declaration"}:
        return replace(state, declared=transition == "readd_declaration")
    if transition == "prune_removed":
        return replace(state, materialized=False, clean=True)
    return state


def applicable_laws(state: LifecycleModel, transition: str) -> frozenset[str]:
    """Return the obligation denominator for a legal action and its post-state."""
    after = advance(state, transition)
    laws = {OPEN_WORLD, OWNERSHIP, ROUTING, OUTCOME}
    if after.materialized:
        laws.add(SOURCE)
    if transition == "reinstall":
        laws.add(IDEMPOTENCY)
    if transition == "audit_tampered":
        laws.add(FAILED_COMMAND)
    if transition in {"dry_run", "audit_clean"}:
        laws.add(READ_ONLY)
    return frozenset(laws)


@dataclass(frozen=True)
class ReplayCase:
    """Explicit transitions survive Hypothesis versions, seeds, and databases."""

    case_id: str
    sequence: tuple[str, ...]
    expected_laws: frozenset[str]
    issue: int | None = None
    fixture: str = FIXTURE_ID


def validate_case(case: ReplayCase) -> None:
    """Reject empty, illegal, or falsely credited replay cases before execution."""
    if not case.case_id or not case.sequence or case.fixture != FIXTURE_ID:
        raise ValueError(f"Invalid replay identity or fixture: {case!r}")
    state = LifecycleModel()
    laws: set[str] = set()
    for index, transition in enumerate(case.sequence):
        try:
            laws.update(applicable_laws(state, transition))
            state = advance(state, transition)
        except ValueError as exc:
            raise ValueError(
                f"case={case.case_id} law=transition.legality "
                f"sequence={json.dumps(case.sequence)} step={index + 1}: {exc}"
            ) from exc
    if laws != case.expected_laws:
        raise ValueError(
            f"case={case.case_id} law=accounting.expected_laws "
            f"sequence={json.dumps(case.sequence)} expected={sorted(laws)} "
            f"declared={sorted(case.expected_laws)}"
        )


def load_corpus(path: Path = CORPUS_PATH) -> tuple[ReplayCase, ...]:
    """Load strict JSON records, without executing Python or seed decoding."""
    payload = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "covering_tour",
        "regressions",
    }:
        raise ValueError("Corpus requires schema_version, covering_tour, regressions")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Unsupported lifecycle corpus schema_version")
    if not isinstance(payload["regressions"], list) or not payload["regressions"]:
        raise ValueError("Corpus must contain explicit regression sequences")
    cases = []
    for row in (payload["covering_tour"], *payload["regressions"]):
        if not isinstance(row, dict) or set(row) != {
            "case_id",
            "fixture",
            "issue",
            "sequence",
            "expected_laws",
        }:
            raise ValueError("Malformed lifecycle replay record")
        if (
            not isinstance(row["case_id"], str)
            or not isinstance(row["sequence"], list)
            or not all(isinstance(item, str) for item in row["sequence"])
            or not isinstance(row["expected_laws"], list)
            or not all(isinstance(item, str) for item in row["expected_laws"])
            or len(row["expected_laws"]) != len(set(row["expected_laws"]))
            or (row["issue"] is not None and type(row["issue"]) is not int)
        ):
            raise ValueError("Malformed lifecycle replay field")
        case = ReplayCase(
            case_id=row["case_id"],
            sequence=tuple(row["sequence"]),
            expected_laws=frozenset(row["expected_laws"]),
            issue=row["issue"],
            fixture=row["fixture"],
        )
        validate_case(case)
        cases.append(case)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Duplicate lifecycle replay case identity")
    if set(cases[0].sequence) != set(TRANSITIONS) or cases[0].expected_laws != LAWS:
        raise ValueError("Covering tour must reach every transition and every law")
    return tuple(cases)
