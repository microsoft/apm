"""Cross-cutting lifecycle oracle: laws every CLI step must keep (#4031).

``check_cross_cutting(before, after, result, model, op)`` runs after every
generated step. Each law aliases the lifecycle-ledger properties it refines
(``tests/fixtures/lifecycle_bug_ledger.json``); laws with no ledger property
are output contracts the ledger does not cover.

A law violation that a model declares as a known failing cell is a STRICT
expected failure: it must keep failing on exactly the steps the model names,
so a fix flips it to an unexpected pass that fails the run.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

LEDGER_PATH = Path(__file__).parents[1] / "fixtures" / "lifecycle_bug_ledger.json"


@dataclass(frozen=True)
class OracleLaw:
    """One cross-cutting law and the ledger properties it aliases."""

    title: str
    ledger_properties: tuple[str, ...]


ORACLE_LAWS: Mapping[str, OracleLaw] = {
    "I1": OracleLaw("lock and deployed files agree", ("outcome.status_matches_state",)),
    "I2": OracleLaw("a converged operation repeats as a byte no-op", ("idempotency.byte_stable",)),
    "I3": OracleLaw(
        "install --frozen succeeds iff audit --ci is clean",
        ("outcome.status_matches_state", "lockfile.frozen_preserves_bytes"),
    ),
    "I4": OracleLaw(
        "deny, revoke and tightening never widen what is deployed",
        ("routing.authorized_targets_only",),
    ),
    "I5": OracleLaw(
        "each command writes only its declared write set",
        ("filesystem.open_world_observation", "ownership.preserve_unowned"),
    ),
    "I6": OracleLaw("output is ASCII; JSON mode prints valid JSON", ()),
    "I7": OracleLaw(
        "no silent success; a failed command preserves durable state",
        ("transaction.failed_command_preserves_state",),
    ),
    "I8": OracleLaw("exit code is in {0, 1, 2}", ("outcome.status_matches_state",)),
    "I9": OracleLaw("no duplicated diagnostics", ()),
    "R": OracleLaw(
        "the CLI agrees with the reference gate model", ("outcome.status_matches_state",)
    ),
}

# Bookkeeping files any command may touch without changing durable state.
_BOOKKEEPING_NAMES = frozenset({".apm-lifecycle.lock", "last_version_check", "device-id"})
# First-run state any command may CREATE (never change): the user config and
# the version-check cache directory.
_FIRST_RUN_CREATES = frozenset({".apm/config.json", "config.json", ".cache", ".cache/apm"})


def _durable_changes(before, after) -> set[str]:
    """Changed paths of one root, without bookkeeping or first-run creations."""
    difference = before.diff(after)
    return {
        path
        for path in difference.added | difference.removed | difference.changed
        if path.rsplit("/", 1)[-1] not in _BOOKKEEPING_NAMES
        and not (path in difference.added and path in _FIRST_RUN_CREATES)
    }


@dataclass(frozen=True)
class Observation:
    """Durable state plus the deployed gate surfaces at one point in time."""

    state: LifecycleStateSnapshot
    artifacts: ArtifactSnapshotSet
    deployed: frozenset[str]
    deploy_root: Path
    lock: LockFile | None
    managed_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Operation:
    """One machine step as the oracle sees it."""

    name: str
    cell_kinds: tuple[str, ...] = ()
    mutating: bool = False
    read_only: bool = False
    tightening: bool = False
    json_output: bool = False
    # Root id -> path prefixes this operation must never touch ("*" is all),
    # unless a path also falls under one of the root's ``allowed`` prefixes.
    forbidden: Mapping[str, Collection[str]] = field(default_factory=dict)
    allowed: Mapping[str, Collection[str]] = field(default_factory=dict)
    # Exit codes of the paired probe; {"audit": rc} pairs an install --frozen with audit --ci.
    paired: Mapping[str, int] = field(default_factory=dict)
    # True when the step only edits inputs (manifest, stores, policy cache).
    edit_only: bool = False


@dataclass(frozen=True)
class Violation:
    law: str
    message: str


@dataclass(frozen=True)
class ExpectedCell:
    """A strict expected failure: violations whose message matches ``match``.

    Only matching violations are excused; any other violation of the same law
    still fails. With ``per_step`` the cell must fail on this step; otherwise
    the caller proves at run level that it still fails somewhere.
    """

    cell_id: str
    match: str
    per_step: bool = True


def partition_expected(
    messages: Collection[str], cells: Collection[ExpectedCell]
) -> tuple[list[str], set[str]]:
    """Split *messages* into unexpected ones and the ids of cells that owned one."""
    unexpected: list[str] = []
    owners: set[str] = set()
    for message in messages:
        hits = {cell.cell_id for cell in cells if re.search(cell.match, message)}
        if hits:
            owners |= hits
        else:
            unexpected.append(message)
    return unexpected, owners


def strict_misses(cells: Collection[ExpectedCell], owners: Collection[str]) -> list[str]:
    """Per-step cells that did not fail on this step (an unexpected pass)."""
    return [cell.cell_id for cell in cells if cell.per_step and cell.cell_id not in owners]


class OracleModel(Protocol):
    """What the oracle needs from a reference model."""

    def allowed_surfaces(self) -> frozenset[str]: ...

    def converged_before(self) -> bool: ...

    def expected_cells(
        self, op: Operation, before: Observation, after: Observation, result: CommandResult | None
    ) -> Mapping[str, tuple[ExpectedCell, ...]]: ...

    def record_expected(self, cell_id: str) -> None: ...


def ledger_property_ids() -> frozenset[str]:
    """Return the property ids the oracle laws may alias."""
    ledger = json.loads(LEDGER_PATH.read_text(encoding="ascii"))
    return frozenset(row["id"] for row in ledger["property_catalog"])


def check_cross_cutting(
    before: Observation,
    after: Observation,
    result: CommandResult | None,
    model: OracleModel,
    op: Operation,
) -> None:
    """Check I1-I9 for one step; honor strict expected failures from *model*."""
    violations = _violations(before, after, result, model, op)
    expected = dict(model.expected_cells(op, before, after, result))
    by_law: dict[str, list[Violation]] = {}
    for violation in violations:
        by_law.setdefault(violation.law, []).append(violation)
    problems: list[str] = []
    for law in sorted(set(by_law) | set(expected)):
        messages = [item.message for item in by_law.get(law, ())]
        cells = expected.get(law, ())
        unexpected, owners = partition_expected(messages, cells)
        problems.extend(f"{law} ({ORACLE_LAWS[law].title}): {message}" for message in unexpected)
        problems.extend(
            f"{law}: strict expected failure {cell_id} did not fail on {op.name}; "
            "the cell now passes, so remove its expectation"
            for cell_id in strict_misses(cells, owners)
        )
        for cell_id in owners:
            model.record_expected(cell_id)
    assert not problems, f"after {op.name}:\n" + "\n".join(problems) + _evidence(result)


def _evidence(result: CommandResult | None) -> str:
    if result is None:
        return ""
    return f"\nargs={result.command!r} rc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _violations(
    before: Observation,
    after: Observation,
    result: CommandResult | None,
    model: OracleModel,
    op: Operation,
) -> list[Violation]:
    found: list[Violation] = []
    for check in _CHECKS:
        found.extend(check(before, after, result, model, op))
    return found


def _i1_lock_deployed(
    before: Observation, after: Observation, result, model, op: Operation
) -> list[Violation]:
    del before, result, model, op
    lock = after.lock
    recorded: dict[str, str | None] = {}
    if lock is not None:
        for record in lock.deployment_ledger.records.values():
            if record.locator.kind.value == "project-relative":
                recorded[record.locator.value] = record.content_hash
        for dependency in lock.get_package_dependencies():
            recorded.update(dependency.deployed_file_hashes)
        recorded.update(lock.local_deployed_file_hashes)
    found = []
    for relative, digest in sorted(recorded.items()):
        path = after.deploy_root / relative
        if not path.is_file():
            found.append(Violation("I1", f"recorded file is missing: {relative}"))
        elif digest and digest != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
            found.append(Violation("I1", f"recorded hash does not match disk: {relative}"))
    for relative in sorted(after.managed_paths - set(recorded)):
        if (after.deploy_root / relative).exists():
            found.append(Violation("I1", f"deployed file is not in the lock: {relative}"))
    return found


def _i2_idempotence(
    before: Observation, after: Observation, result, model: OracleModel, op: Operation
) -> list[Violation]:
    if not (op.mutating and model.converged_before()):
        return []
    if result is not None and result.returncode != 0:
        return [Violation("I2", f"converged {op.name} exited {result.returncode}")]
    found = []
    if after.state.semantic_bytes != before.state.semantic_bytes:
        found.append(Violation("I2", "lock or manifest semantics changed on a converged state"))
    changed = _changed_paths(before.artifacts, after.artifacts)
    if changed:
        found.append(Violation("I2", f"converged {op.name} rewrote files: {changed}"))
    return found


def _i3_frozen_audit(
    before: Observation, after: Observation, result, model, op: Operation
) -> list[Violation]:
    del before, after, model
    if "audit" not in op.paired or result is None:
        return []
    audit, frozen = op.paired["audit"], result.returncode
    if (audit == 0) != (frozen == 0):
        return [Violation("I3", f"audit --ci exited {audit} but install --frozen exited {frozen}")]
    return []


def _i4_gate_monotonicity(
    before: Observation, after: Observation, result, model: OracleModel, op: Operation
) -> list[Violation]:
    """I4 relative to the model: after each successful mutating step, nothing is
    deployed that the model's current approvals and policy do not allow.

    A deny, revoke or tightening shrinks the allowed set, so the next mutating
    step must retire what it no longer allows. One violation per surface, so a
    known cell never hides another surface.
    """
    del before
    if not op.mutating or (result is not None and result.returncode != 0):
        return []
    return [
        Violation("I4", f"{surface} still deployed but not allowed")
        for surface in sorted(after.deployed - model.allowed_surfaces())
    ]


def _i5_write_set(
    before: Observation, after: Observation, result, model, op: Operation
) -> list[Violation]:
    del result, model
    found = []
    for root_id, snapshot in before.artifacts.snapshots:
        observed = _durable_changes(snapshot, after.artifacts.snapshot(root_id))
        allowed = op.allowed.get(root_id, ())
        observed -= {path for path in observed if any(_within(path, a) for a in allowed)}
        for prefix in op.forbidden.get(root_id, ()):
            hit = sorted(path for path in observed if _within(path, prefix))
            if hit:
                found.append(Violation("I5", f"{op.name} wrote {root_id}:{hit}"))
        if op.read_only and observed:
            found.append(Violation("I5", f"read-only {op.name} wrote {root_id}:{sorted(observed)}"))
    return found


def _i6_ascii(before, after, result: CommandResult | None, model, op: Operation) -> list[Violation]:
    del before, after, model
    if result is None:
        return []
    found = []
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if not text.isascii():
            bad = sorted({hex(ord(char)) for char in text if ord(char) > 0x7E})[:5]
            found.append(Violation("I6", f"{stream} has non-ASCII characters {bad}"))
    if op.json_output and result.returncode in (0, 1):
        try:
            json.loads(result.stdout)
        except ValueError as exc:
            found.append(Violation("I6", f"JSON mode printed invalid JSON: {exc}"))
    return found


def _i7_failure_preserves_state(
    before: Observation, after: Observation, result: CommandResult | None, model, op: Operation
) -> list[Violation]:
    del model
    if result is None or result.returncode == 0 or op.edit_only:
        return []
    found = []
    if after.state.manifest_bytes != before.state.manifest_bytes:
        found.append(Violation("I7", f"failed {op.name} changed the manifest"))
    if after.state.lockfile_bytes != before.state.lockfile_bytes:
        found.append(Violation("I7", f"failed {op.name} changed the lockfile"))
    if after.deployed != before.deployed:
        found.append(Violation("I7", f"failed {op.name} changed deployed surfaces"))
    return found


def _i8_exit_code(before, after, result: CommandResult | None, model, op) -> list[Violation]:
    del before, after, model, op
    if result is None or result.returncode in (0, 1, 2):
        return []
    return [Violation("I8", f"exit code {result.returncode} is outside {{0, 1, 2}}")]


def _i9_no_noise(before, after, result: CommandResult | None, model, op) -> list[Violation]:
    del before, after, model, op
    if result is None:
        return []
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    found = []
    for first, second in itertools.pairwise(lines):
        if first == second:
            found.append(Violation("I9", f"identical consecutive line: {first.strip()!r}"))
            break
    integrated = [line.strip() for line in lines if "integrated" in line]
    if len(integrated) != len(set(integrated)):
        found.append(Violation("I9", "duplicate 'integrated' line"))
    return found


_CHECKS: tuple[Callable[..., list[Violation]], ...] = (
    _i1_lock_deployed,
    _i2_idempotence,
    _i3_frozen_audit,
    _i4_gate_monotonicity,
    _i5_write_set,
    _i6_ascii,
    _i7_failure_preserves_state,
    _i8_exit_code,
    _i9_no_noise,
)


def _changed_paths(before: ArtifactSnapshotSet, after: ArtifactSnapshotSet) -> list[str]:
    """Changed files, comparing the lockfile semantically (generation time may move)."""
    changed = []
    for root_id, snapshot in before.snapshots:
        for path in sorted(_durable_changes(snapshot, after.snapshot(root_id))):
            if path.rsplit("/", 1)[-1] != "apm.lock.yaml":
                changed.append(f"{root_id}:{path}")
    return changed


def _within(path: str, prefix: str) -> bool:
    if prefix == "*":
        return True
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")
