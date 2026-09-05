"""Deterministic lifecycle obligations, independent of execution and output ledgers."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache

from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile

GENERATOR_VERSION = 1
Factor = tuple[str, str]
Interaction = tuple[Factor, ...]


@dataclass(frozen=True)
class RoutingRow:
    """Source inputs and action intent for one real lifecycle scenario."""

    id: str
    primitives: tuple[str, ...]
    targets: tuple[str, ...]
    user_scope: bool
    catalog_cell: bool = False
    dynamic_refusal: bool = False
    widen_targets: tuple[str, ...] = ()
    narrow_targets: tuple[str, ...] = ()
    dependency_shape: str = "direct"
    source_kind: str = "git"
    ref_state: str = "pinned"
    cache_state: str = "cold"
    integrity_state: str = "clean"
    command: str = "reinstall"


def catalog_rows() -> tuple[RoutingRow, ...]:
    """Enumerate the live static catalog without resolving external user roots."""
    rows = []
    for name, base in sorted(KNOWN_TARGETS.items()):
        if base.user_root_resolver is not None:
            continue
        for user_scope in (False, True):
            profile = base.for_scope(user_scope=user_scope)
            if profile is None:
                continue
            scope = "user" if user_scope else "project"
            rows.extend(
                RoutingRow(
                    f"{name}-{primitive}-{scope}",
                    (primitive,),
                    (name,),
                    user_scope,
                    catalog_cell=True,
                )
                for primitive in sorted(profile.primitives)
            )
    return tuple(rows)


TRANSITION_ROWS = (
    RoutingRow(
        "copilot-prompt-widen-narrow",
        ("prompts",),
        ("copilot",),
        False,
        widen_targets=("copilot", "cursor"),
        narrow_targets=("copilot",),
        command="widen-narrow",
    ),
    RoutingRow(
        "claude-codex-hook-narrow",
        ("hooks",),
        ("claude", "codex"),
        False,
        narrow_targets=("claude",),
        command="narrow",
    ),
    RoutingRow(
        "claude-codex-hook-widen-narrow-user",
        ("hooks",),
        ("claude",),
        True,
        widen_targets=("claude", "codex"),
        narrow_targets=("claude",),
        command="widen-narrow",
    ),
)
DYNAMIC_REFUSAL_ROWS = (
    RoutingRow(
        "copilot-app-unavailable",
        ("prompts",),
        ("copilot-app",),
        True,
        dynamic_refusal=True,
        command="refusal",
    ),
    RoutingRow(
        "copilot-cowork-unavailable",
        ("skills",),
        ("copilot-cowork",),
        True,
        dynamic_refusal=True,
        command="refusal",
    ),
)
ROUTING_ROWS = (*catalog_rows(), *TRANSITION_ROWS, *DYNAMIC_REFUSAL_ROWS)


def row_profiles(row: RoutingRow) -> tuple[TargetProfile, ...]:
    """Resolve supported fixture layouts, not observed deployment records."""
    profiles = []
    for target in row.targets:
        profile = KNOWN_TARGETS[target]
        if not row.dynamic_refusal:
            profile = profile.for_scope(user_scope=row.user_scope)
            assert profile is not None, f"{row.id}: unavailable scope for {target}"
        assert all(profile.supports(kind) for kind in row.primitives), row.id
        profiles.append(profile)
    return tuple(profiles)


def feature_flags(row: RoutingRow) -> tuple[str, ...]:
    """Return declared prerequisites for the actual row."""
    flags = {"canvas"} if "canvas" in row.primitives else set()
    for target in row.targets:
        flag = KNOWN_TARGETS[target].requires_flag
        if flag is not None:
            flags.add(flag)
    return tuple(sorted(flags))


def expected_ledger_targets(row: RoutingRow, targets: tuple[str, ...] | None = None) -> set[str]:
    """Resolve canonical names for the requested, supported fixture targets."""
    owners = set()
    for target in row.targets if targets is None else targets:
        profile = KNOWN_TARGETS[target].for_scope(user_scope=row.user_scope)
        assert profile is not None
        if any(kind in profile.primitives for kind in row.primitives):
            owners.add(profile.name)
    return owners


def validate_routing_rows(rows: Sequence[RoutingRow]) -> None:
    """Require real catalog cells and the named non-vacuous transition obligations."""
    ids = [row.id for row in rows]
    assert len(ids) == len(set(ids)), "Duplicate lifecycle case IDs"
    expected = {(row.targets, row.primitives, row.user_scope) for row in catalog_rows()}
    actual = [(row.targets, row.primitives, row.user_scope) for row in rows if row.catalog_cell]
    assert len(actual) == len(set(actual)), "Duplicate catalog cells"
    assert set(actual) == expected, "Missing or unexpected catalog cells"
    indexed = {row.id: row for row in rows}
    for required in (*TRANSITION_ROWS, *DYNAMIC_REFUSAL_ROWS):
        assert indexed.get(required.id) == required, f"Missing obligation: {required.id}"
    assert {row.targets[0] for row in DYNAMIC_REFUSAL_ROWS} == {
        name for name, profile in KNOWN_TARGETS.items() if profile.user_root_resolver is not None
    }, "New dynamic target requires an explicit refusal/success fixture decision"


FACTOR_DOMAINS = {
    "dependency_shape": ("direct", "transitive"),
    "source_kind": ("git", "local"),
    "ref_state": ("pinned", "tag", "none"),
    "cache_state": ("cold", "warm", "none"),
    "integrity_state": ("clean", "tampered"),
    "command": ("reinstall", "audit", "update"),
}
EXCLUSIONS = (
    {
        "id": "local-ref-cache",
        "reason": "Local paths have no Git ref or fetch-cache state; both use 'none'.",
    },
    {
        "id": "external-target-success",
        "reason": "Runtime-resolved target success needs a hermetic external-root fixture.",
    },
    {
        "id": "source-domain",
        "reason": (
            "This campaign covers Git and local packages, not marketplace/OCI sources or "
            "virtual packages. Existing shape/source suites do not count as executed witnesses here."
        ),
    },
    {
        "id": "service-configuration",
        "reason": "MCP/LSP-only dependencies are not primitive catalog cells.",
    },
)


def invalid_reason(row: RoutingRow) -> str | None:
    """State semantic constraints explicitly; never constrain by generation cost."""
    for name, values in FACTOR_DOMAINS.items():
        if getattr(row, name) not in values:
            return f"Unknown {name}: {getattr(row, name)}"
    if len(row.targets) != 1 or len(row.primitives) != 1:
        return "Interaction cases require one static catalog cell"
    base = KNOWN_TARGETS.get(row.targets[0])
    if base is None or base.user_root_resolver is not None:
        return "Interaction target must have a static fixture root"
    profile = base.for_scope(user_scope=row.user_scope)
    if profile is None or not profile.supports(row.primitives[0]):
        return "Unsupported primitive/target/scope cell"
    if row.source_kind == "local":
        if row.ref_state != "none" or row.cache_state != "none":
            return "Local path source has no Git ref or fetch-cache state"
    elif row.ref_state == "none" or row.cache_state == "none":
        return "Git source requires an explicit ref and fetch-cache state"
    return None


def factors(row: RoutingRow) -> tuple[Factor, ...]:
    """Return concrete input values, with scope and target represented separately."""
    return tuple(
        sorted(
            {
                "primitive": ",".join(row.primitives),
                "target": ",".join(row.targets),
                "scope": "user" if row.user_scope else "project",
                **{name: getattr(row, name) for name in FACTOR_DOMAINS},
            }.items()
        )
    )


def interactions(row: RoutingRow, strength: int = 2) -> frozenset[Interaction]:
    """Project a concrete row onto all interactions of the requested strength."""
    return frozenset(itertools.combinations(factors(row), strength))


@lru_cache(maxsize=1)
def candidate_rows() -> tuple[RoutingRow, ...]:
    """Enumerate finite legal inputs; no subprocesses or fixture-derived exclusions."""
    rows = []
    names = tuple(FACTOR_DOMAINS)
    variants = tuple(itertools.product(*FACTOR_DOMAINS.values()))
    for cell in catalog_rows():
        for values in variants:
            row = replace(cell, catalog_cell=False, **dict(zip(names, values, strict=True)))
            if invalid_reason(row) is not None:
                continue
            encoded = json.dumps(factors(row), separators=(",", ":")).encode("ascii")
            rows.append(replace(row, id=f"interaction-{hashlib.sha256(encoded).hexdigest()[:16]}"))
    return tuple(rows)


@dataclass(frozen=True)
class TripleObligation:
    """A reviewed risk combination, not a claim to reproduce the cited incident."""

    id: str
    values: Interaction
    issue: int | None


HIGH_RISK_TRIPLES = (
    TripleObligation(
        "aliased-ref-warm-update",
        (("cache_state", "warm"), ("command", "update"), ("ref_state", "tag")),
        2484,
    ),
    TripleObligation(
        "user-instructions-reinstall",
        (("command", "reinstall"), ("primitive", "instructions"), ("scope", "user")),
        None,
    ),
    TripleObligation(
        "tampered-transitive-audit",
        (("command", "audit"), ("dependency_shape", "transitive"), ("integrity_state", "tampered")),
        None,
    ),
)


def _triple_tokens(row: RoutingRow) -> frozenset[Interaction]:
    values = set(factors(row))
    return frozenset(
        tuple(sorted(triple.values)) for triple in HIGH_RISK_TRIPLES if set(triple.values) <= values
    )


@lru_cache(maxsize=1)
def required_interactions() -> frozenset[Interaction]:
    """Compute the denominator from every legal input, not the selected rows."""
    return frozenset().union(*(interactions(row) for row in candidate_rows()))


@lru_cache(maxsize=1)
def generate_interaction_rows() -> tuple[RoutingRow, ...]:
    """Greedily cover legal pairs and named triples with stable tie-breaking."""
    universe = sorted(
        required_interactions()
        | frozenset(tuple(sorted(triple.values)) for triple in HIGH_RISK_TRIPLES)
    )
    bits = {item: 1 << index for index, item in enumerate(universe)}

    def mask(row: RoutingRow) -> int:
        return sum(bits[item] for item in interactions(row) | _triple_tokens(row) if item in bits)

    remaining = (1 << len(bits)) - 1
    for row in ROUTING_ROWS:
        remaining &= ~mask(row)
    candidates = [(row, mask(row)) for row in candidate_rows()]
    selected = []
    while remaining:
        index = max(
            range(len(candidates)), key=lambda i: (candidates[i][1] & remaining).bit_count()
        )
        row, covered = candidates.pop(index)
        if not covered & remaining:
            raise AssertionError("No executable candidate covers the remaining obligations")
        selected.append(row)
        remaining &= ~covered
    return tuple(selected)


INTERACTION_ROWS = generate_interaction_rows()
KNOWN_IDEMPOTENCY_GAP = "copilot-user-instruction-second-pass-convergence"


def validate_campaign(rows: Sequence[RoutingRow]) -> None:
    """Validate selected inputs independently of the selection algorithm."""
    validate_routing_rows(rows)
    for row in rows:
        if row.id.startswith("interaction-"):
            assert invalid_reason(row) is None, f"{row.id}: {invalid_reason(row)}"
    covered = frozenset().union(*(interactions(row) for row in rows))
    missing = required_interactions() - covered
    assert not missing, f"Uncovered valid pairs: {sorted(missing)}"
    for triple in HIGH_RISK_TRIPLES:
        assert any(set(triple.values) <= set(factors(row)) for row in rows), triple.id


@dataclass(frozen=True)
class CaseExecution:
    """Execution evidence; selection alone must never earn coverage credit."""

    case_id: str
    status: str
    evaluated_laws: tuple[str, ...]
    transitions: tuple[str, ...]
    duration_seconds: float
    reason: str | None = None


def required_laws(row: RoutingRow) -> frozenset[str]:
    """Declare which promises must execute before crediting an input combination."""
    laws = {
        "filesystem.open_world_observation",
        "outcome.status_matches_state",
    }
    if row.dynamic_refusal:
        return frozenset({*laws, "transaction.failed_command_preserves_state"})
    laws.update({"ownership.preserve_unowned", "routing.authorized_targets_only"})
    if row.command in {"reinstall", "update", "widen-narrow", "narrow"}:
        laws.add("idempotency.byte_stable")
    if row.id.startswith("interaction-"):
        laws.add("source.ref_cache_coherent")
    return frozenset(laws)


def required_transitions(row: RoutingRow) -> frozenset[str]:
    """Declare action witnesses without equating setup with the requested action."""
    if row.dynamic_refusal:
        return frozenset({"install", "refusal"})
    transitions = {"install", "uninstall", row.command}
    if row.widen_targets:
        transitions.update({"widen", "reinstall-widened"})
    if row.narrow_targets:
        transitions.add("narrow")
        if not row.user_scope:
            transitions.add("prune")
    if row.integrity_state == "tampered":
        transitions.add("tamper")
    if known_gap_for(row) is not None:
        transitions.add("converge-known-gap")
    return frozenset(transitions - {"widen-narrow"})


def known_gap_for(row: RoutingRow) -> str | None:
    """Name the existing, bounded product defect without relaxing other promises."""
    if (
        row.id == "copilot-instructions-user"
        and row.targets == ("copilot",)
        and row.primitives == ("instructions",)
        and row.user_scope
        and row.source_kind == "git"
        and row.command == "reinstall"
        and row.integrity_state == "clean"
        and row.dependency_shape == "direct"
    ):
        return KNOWN_IDEMPOTENCY_GAP
    return None


def coverage_report(
    rows: Sequence[RoutingRow],
    executions: Iterable[CaseExecution],
    *,
    input_revision: str,
) -> dict[str, object]:
    """Report planned and observed coverage separately with deterministic witnesses."""
    validate_campaign(rows)
    indexed = {row.id: row for row in rows}
    observed: dict[str, CaseExecution] = {}
    witnesses: dict[Interaction, list[str]] = {}
    rejected = {}
    for execution in executions:
        if execution.case_id not in indexed or execution.case_id in observed:
            raise ValueError(f"Unknown or duplicate execution: {execution.case_id}")
        if execution.status not in {"executed", "skipped", "setup_failed", "failed", "known_gap"}:
            raise ValueError(f"Unknown execution status: {execution.status}")
        if not math.isfinite(execution.duration_seconds) or execution.duration_seconds < 0:
            raise ValueError("Execution duration must be finite and nonnegative")
        row = indexed[execution.case_id]
        observed[row.id] = execution
        missing_laws = required_laws(row) - set(execution.evaluated_laws)
        missing_steps = required_transitions(row) - set(execution.transitions)
        if execution.status != "executed" or missing_laws or missing_steps:
            rejected[row.id] = {
                "status": execution.status,
                "missing_laws": sorted(missing_laws),
                "missing_transitions": sorted(missing_steps),
                "reason": execution.reason,
            }
            continue
        for item in interactions(row) | _triple_tokens(row):
            witnesses.setdefault(item, []).append(row.id)
    required = required_interactions()
    catalog = [(row.targets, row.primitives, row.user_scope) for row in catalog_rows()]
    return {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "input_revision": input_revision,
        "catalog_fingerprint": hashlib.sha256(
            json.dumps(catalog, sort_keys=True).encode("ascii")
        ).hexdigest(),
        "scheduled_case_ids": sorted(indexed),
        "unexecuted_case_ids": sorted(set(indexed) - set(observed)),
        "rejected_evidence": dict(sorted(rejected.items())),
        "valid_pairs": sorted(required),
        "covered_pairs": sorted(required & witnesses.keys()),
        "uncovered_pairs": sorted(required - witnesses.keys()),
        "triples": [
            {
                "id": triple.id,
                "issue": triple.issue,
                "values": sorted(triple.values),
                "witnesses": sorted(witnesses.get(tuple(sorted(triple.values)), ())),
            }
            for triple in HIGH_RISK_TRIPLES
        ],
        "witnesses": [
            {"values": values, "case_ids": sorted(case_ids)}
            for values, case_ids in sorted(witnesses.items())
            if values in required
        ],
        "durations_seconds": {
            name: evidence.duration_seconds for name, evidence in sorted(observed.items())
        },
        "exclusions": EXCLUSIONS,
    }


def assert_complete_evidence(
    rows: Sequence[RoutingRow], executions: Sequence[CaseExecution]
) -> None:
    """Require all obligations, allowing only the named idempotency characterization."""
    report = coverage_report(rows, executions, input_revision="evidence-validation")
    assert not report["unexecuted_case_ids"], (
        f"Missing lifecycle execution evidence: {report['unexecuted_case_ids']}"
    )
    indexed = {row.id: row for row in rows}
    permitted_pairs: set[Interaction] = set()
    for execution in executions:
        if execution.case_id not in report["rejected_evidence"]:
            continue
        row = indexed[execution.case_id]
        missing_laws = required_laws(row) - set(execution.evaluated_laws)
        assert (
            execution.status == "known_gap"
            and execution.reason == known_gap_for(row)
            and execution.reason is not None
            and missing_laws <= {"idempotency.byte_stable"}
            and not (required_transitions(row) - set(execution.transitions))
        ), f"Unmet lifecycle obligation {row.id}: {report['rejected_evidence'][row.id]}"
        permitted_pairs.update(interactions(row))
    assert set(report["uncovered_pairs"]) <= permitted_pairs, (
        "Uncovered pairs are not explained by the explicit idempotency gap"
    )
    missing_triples = [triple["id"] for triple in report["triples"] if not triple["witnesses"]]
    assert not missing_triples, f"Uncovered high-risk triples: {missing_triples}"


def execution_from_mapping(payload: Mapping[str, object]) -> CaseExecution:
    """Reject malformed artifact fields before treating them as executed evidence."""
    case_id, status = payload.get("case_id"), payload.get("status")
    if not isinstance(case_id, str) or not isinstance(status, str):
        raise ValueError("Execution case_id and status must be strings")
    laws, transitions = payload.get("evaluated_laws"), payload.get("transitions")
    if not isinstance(laws, list) or not all(isinstance(item, str) for item in laws):
        raise ValueError("Execution evaluated_laws must be a string array")
    if not isinstance(transitions, list) or not all(isinstance(item, str) for item in transitions):
        raise ValueError("Execution transitions must be a string array")
    duration = payload.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ValueError("Execution duration_seconds must be a number")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Execution reason must be a string or null")
    return CaseExecution(
        case_id=case_id,
        status=status,
        evaluated_laws=tuple(laws),
        transitions=tuple(transitions),
        duration_seconds=float(duration),
        reason=reason,
    )
