"""Cross-cutting lifecycle oracle: laws every CLI step must keep (#4031, #4093).

``check_cross_cutting(before, after, result, model, op)`` runs after every
generated step. Each law aliases the lifecycle-ledger properties it refines
(``tests/fixtures/lifecycle_bug_ledger.json``); laws with no ledger property
are output contracts the ledger does not cover.

A law violation that a model declares as a known failing cell is a STRICT
expected failure: it must keep failing on exactly the steps the model names,
so a fix flips it to an unexpected pass that fails the run.

Every law that applies to a step and passes writes exactly one receipt
(``tests/lifecycle/receipts.py``). A law that does not apply, fails, or is
owned by a strict expected failure earns nothing; receipts are the only
credit ``tests/quality/test_promise_obligations.py`` accepts.
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
from tests.lifecycle import receipts
from tests.lifecycle.model.expected import exit_contract
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

LEDGER_PATH = Path(__file__).parents[1] / "fixtures" / "lifecycle_bug_ledger.json"

# Machine step names -> the OP factor of the obligation vocabulary.
_OP_FAMILIES: Mapping[str, str] = {
    "install-frozen": "install",
    "approve": "approve-deny",
    "deny": "approve-deny",
    "audit-ci": "audit",
}


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
    "I8": OracleLaw(
        "exit code is in the command's documented contract", ("outcome.status_matches_state",)
    ),
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
    # Service -> server names configured in the managed adapter configs
    # ({"lsp": {"kit-lsp"}}); I1 reconciles them with the lock's URI claims.
    services: Mapping[str, frozenset[str]] = field(default_factory=dict)


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
    # Output (stdout + stderr) of each paired probe; I3 asks a failing side for a remediation.
    paired_output: Mapping[str, str] = field(default_factory=dict)
    # True when the step only edits inputs (manifest, stores, policy cache).
    edit_only: bool = False
    # CLI command for the exit contract (I8); defaults to the first word of ``name``.
    command: str = ""
    # Obligation cells this step visits (for example GATE cells); copied into receipts.
    cells: tuple[str, ...] = ()
    scope: str = "project"
    # Modifier factors of the obligation vocabulary (FROZEN, DRYRUN, FORCE, ...).
    modifiers: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        """The OP factor this step exercises (``install-frozen`` -> ``install``)."""
        return _OP_FAMILIES.get(self.name, self.name)

    @property
    def exit_contract(self) -> frozenset[int]:
        return exit_contract(self.command or self.name.split("-", 1)[0])


_MODIFIER_FLAGS = {
    "--frozen": "FROZEN",
    "--dry-run": "DRYRUN",
    "--force": "FORCE",
    "--no-policy": "NOPOLICY",
}
# Commands that may change durable state (manifest, lock, deployed files, stores).
MUTATING_COMMANDS = frozenset(
    {"install", "update", "uninstall", "prune", "compile", "lock", "approve", "deny"}
)


def operation_for(args: tuple[str, ...], *, scope: str = "project", **fields: object) -> Operation:
    """The ``Operation`` one CLI command line exercises (``args`` without ``apm``)."""
    command = args[0]
    modifiers = tuple(sorted({flag for arg, flag in _MODIFIER_FLAGS.items() if arg in args}))
    name = command
    if command == "install" and "FROZEN" in modifiers:
        name = "install-frozen"
    elif command == "audit" and "--ci" in args:
        name = "audit-ci"
    json_output = "--json" in args or any(
        first == "--format" and second == "json" for first, second in itertools.pairwise(args)
    )
    defaults: dict[str, object] = {
        "mutating": command in MUTATING_COMMANDS and "DRYRUN" not in modifiers,
        "json_output": json_output,
    }
    return Operation(
        name, command=command, scope=scope, modifiers=modifiers, **{**defaults, **fields}
    )


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
    """Check I1-I9 for one step; honor strict expected failures from *model*.

    Each applicable law with no violation writes one receipt.
    """
    outcomes = {law: check(before, after, result, model, op) for law, check in _CHECKS.items()}
    _settle(
        outcomes, model.expected_cells(op, before, after, result), model, op, before, after, result
    )


def check_output(result: CommandResult, op: Operation) -> None:
    """Check the output-only laws (I6, I8, I9) for one command result.

    The global output hook in ``tests/lifecycle/conftest.py`` runs this after
    every driver command, so a row that forgets ``check_cross_cutting`` still
    keeps ASCII output, the exit contract and the no-noise law.
    """
    outcomes = {law: _CHECKS[law](None, None, result, None, op) for law in _OUTPUT_LAWS}
    _settle(outcomes, {}, None, op, None, None, result)


def _settle(
    outcomes: Mapping[str, list[Violation] | None],
    expected_cells: Mapping[str, Collection[ExpectedCell]],
    model: OracleModel | None,
    op: Operation,
    before: Observation | None,
    after: Observation | None,
    result: CommandResult | None,
) -> None:
    expected = dict(expected_cells)
    problems: list[str] = []
    passed: list[str] = []
    for law in sorted(set(outcomes) | set(expected)):
        violations = outcomes.get(law)
        messages = [item.message for item in violations or ()]
        cells = expected.get(law, ())
        unexpected, owners = partition_expected(messages, cells)
        problems.extend(f"{law} ({ORACLE_LAWS[law].title}): {message}" for message in unexpected)
        problems.extend(
            f"{law}: strict expected failure {cell_id} did not fail on {op.name}; "
            "the cell now passes, so remove its expectation"
            for cell_id in strict_misses(cells, owners)
        )
        for cell_id in owners:
            assert model is not None
            model.record_expected(cell_id)
        if violations is not None and not messages:
            passed.append(law)
    assert not problems, f"after {op.name}:\n" + "\n".join(problems) + _evidence(result)
    for law in passed:
        _emit_receipt(law, op, before, after, result)


def _emit_receipt(
    law: str,
    op: Operation,
    before: Observation | None,
    after: Observation | None,
    result: CommandResult | None,
) -> None:
    refused = result is not None and result.returncode != 0
    params: dict[str, object] = {
        "op": op.family,
        "step": op.name,
        "modifiers": sorted(op.modifiers),
        "cells": sorted(op.cells),
        # A paired probe (audit --ci beside install --frozen) also witnesses its own op.
        "paired": sorted(_OP_FAMILIES.get(name, name) for name in op.paired),
    }
    if result is not None:
        params["exit"] = result.returncode
    receipts.emit(
        receipts.Receipt(
            node=receipts.current_node(),
            params=params,
            pre_digest=_durable_digest(before),
            argv=tuple(result.command) if result is not None else (),
            scope=op.scope,
            post_digest=_durable_digest(after),
            oracle_id=law,
            outcome="refusal" if refused else "success",
            lane=receipts.current_lane(),
        )
    )


def _durable_digest(observation: Observation | None) -> str:
    """Digest of manifest, lockfile and deployed surfaces: equal pre/post proves non-mutation."""
    if observation is None:
        return ""
    digest = hashlib.sha256()
    for part in (
        observation.state.manifest_bytes or b"",
        observation.state.lockfile_bytes or b"",
        "\n".join(sorted(observation.deployed)).encode("ascii"),
    ):
        digest.update(hashlib.sha256(part).digest())
    return "sha256:" + digest.hexdigest()


def _evidence(result: CommandResult | None) -> str:
    if result is None:
        return ""
    return f"\nargs={result.command!r} rc={result.returncode}\n{result.stdout}\n{result.stderr}"


# Each check returns None when the law does not apply to the step (no credit),
# otherwise the list of violations it found.
_Check = Callable[..., "list[Violation] | None"]


def _i1_lock_deployed(
    before: Observation, after: Observation, result, model, op: Operation
) -> list[Violation] | None:
    del before, result, model, op
    lock = after.lock
    if lock is None and not after.managed_paths and not after.services:
        return None
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
        # A record without a hash may be a directory (a deployed skill); it must exist.
        if not (path.is_file() if digest else path.exists()):
            found.append(Violation("I1", f"recorded file is missing: {relative}"))
        elif digest and digest != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
            found.append(Violation("I1", f"recorded hash does not match disk: {relative}"))
    for relative in sorted(after.managed_paths - set(recorded)):
        if (after.deploy_root / relative).exists():
            found.append(Violation("I1", f"deployed file is not in the lock: {relative}"))
    found.extend(_service_claim_violations(after))
    return found


def _service_claim_violations(after: Observation) -> list[Violation]:
    """Each service claim of the lock names a configured server, and vice versa."""
    records = after.lock.deployment_ledger.records.values() if after.lock is not None else ()
    found = []
    for service, configured in sorted(after.services.items()):
        claimed = {
            record.locator.value
            for record in records
            if record.locator.kind.value == "uri" and record.locator.target == service
        }
        found.extend(
            Violation("I1", f"claimed {service} server is not configured: {server}")
            for server in sorted(claimed - configured)
        )
        found.extend(
            Violation("I1", f"configured {service} server is not claimed: {server}")
            for server in sorted(configured - claimed)
        )
    return found


def _i2_idempotence(
    before: Observation, after: Observation, result, model: OracleModel, op: Operation
) -> list[Violation] | None:
    if not (op.mutating and model.converged_before()):
        return None
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
) -> list[Violation] | None:
    del before, after, model
    if "audit" not in op.paired or result is None:
        return None
    audit, frozen = op.paired["audit"], result.returncode
    found = []
    if (audit == 0) != (frozen == 0):
        found.append(
            Violation("I3", f"audit --ci exited {audit} but install --frozen exited {frozen}")
        )
    # A failing side names the command that repairs the project.
    sides = (
        ("audit --ci", audit, op.paired_output.get("audit")),
        ("install --frozen", frozen, result.stdout + result.stderr),
    )
    for label, code, text in sides:
        if code != 0 and text is not None and not _REMEDIATION.search(" ".join(text.split())):
            found.append(Violation("I3", f"{label} exited {code} without a remediation"))
    return found


# A remediation names an apm command line ("run 'apm install'", "apm approve <pkg>").
_REMEDIATION = re.compile(r"\bapm [a-z][a-z-]+")


def _i4_gate_monotonicity(
    before: Observation, after: Observation, result, model: OracleModel, op: Operation
) -> list[Violation] | None:
    """I4 relative to the model: after each successful mutating step, nothing is
    deployed that the model's current approvals and policy do not allow.

    A deny, revoke or tightening shrinks the allowed set, so the next mutating
    step must retire what it no longer allows. One violation per surface, so a
    known cell never hides another surface.
    """
    del before
    if not op.mutating or (result is not None and result.returncode != 0):
        return None
    return [
        Violation("I4", f"{surface} still deployed but not allowed")
        for surface in sorted(after.deployed - model.allowed_surfaces())
    ]


def _i5_write_set(
    before: Observation, after: Observation, result, model, op: Operation
) -> list[Violation] | None:
    del result, model
    if not (op.forbidden or op.read_only):
        return None
    found = []
    for root_id, snapshot in before.artifacts.snapshots:
        observed = _durable_changes(snapshot, after.artifacts.snapshot(root_id))
        allowed = op.allowed.get(root_id, ())
        # An allowed prefix admits its subtree and its exact ancestor directories.
        observed -= {
            path
            for path in observed
            if any(_within(path, a) or a.startswith(path + "/") for a in allowed)
        }
        for prefix in op.forbidden.get(root_id, ()):
            hit = sorted(path for path in observed if _within(path, prefix))
            if hit:
                found.append(Violation("I5", f"{op.name} wrote {root_id}:{hit}"))
        if op.read_only and observed:
            found.append(Violation("I5", f"read-only {op.name} wrote {root_id}:{sorted(observed)}"))
    return found


def _i6_ascii(
    before, after, result: CommandResult | None, model, op: Operation
) -> list[Violation] | None:
    del before, after, model
    if result is None:
        return None
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
) -> list[Violation] | None:
    del model
    if result is None or result.returncode == 0 or op.edit_only:
        return None
    found = []
    if after.state.manifest_bytes != before.state.manifest_bytes:
        found.append(Violation("I7", f"failed {op.name} changed the manifest"))
    if after.state.lockfile_bytes != before.state.lockfile_bytes:
        found.append(Violation("I7", f"failed {op.name} changed the lockfile"))
    if after.deployed != before.deployed:
        found.append(Violation("I7", f"failed {op.name} changed deployed surfaces"))
    changed = [
        path
        for path in _changed_paths(before.artifacts, after.artifacts)
        if path.split(":", 1)[1].rsplit("/", 1)[-1] not in {"apm.yml", "apm.lock.yaml"}
        and not any(_within(path.split(":", 1)[1], prefix) for prefix in _FAILURE_BOOKKEEPING)
    ]
    if changed:
        found.append(Violation("I7", f"failed {op.name} changed durable files: {changed}"))
    if not _FAILURE_ANNOUNCEMENT.search(result.stdout + result.stderr):
        found.append(
            Violation("I7", f"failed {op.name} exited {result.returncode} without announcing why")
        )
    return found


# Bookkeeping a failed command may still write: receipts, logs, caches, CLI state.
_FAILURE_BOOKKEEPING = (
    ".apm/deployment-receipts",
    ".apm/logs",
    ".apm-local",
    ".local/state/gh",
    ".cache/apm",
)
# A failed command announces its failure: an [x] line or an error/failure word.
_FAILURE_ANNOUNCEMENT = re.compile(
    r"\[x\]|\berror\b|\bfail|\bdenied\b|\bnot found\b|\babort|\bcannot\b|\binvalid\b|\brefus",
    re.I,
)


def _i8_exit_code(
    before, after, result: CommandResult | None, model, op: Operation
) -> list[Violation] | None:
    del before, after, model
    if result is None:
        return None
    contract = op.exit_contract
    if result.returncode in contract:
        return []
    return [Violation("I8", f"exit code {result.returncode} is outside {sorted(contract)}")]


def _i9_no_noise(before, after, result: CommandResult | None, model, op) -> list[Violation] | None:
    """One violation per distinct duplicated line, so a cell for one cause never hides another."""
    del before, after, model, op
    if result is None:
        return None
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    found = []
    consecutive = dict.fromkeys(
        first.strip() for first, second in itertools.pairwise(lines) if first == second
    )
    found.extend(Violation("I9", f"identical consecutive line: {line!r}") for line in consecutive)
    # Each package block ("[+] <package>") reports its own integrations, so the
    # same line under two packages is not noise; twice in one block is.
    seen: set[str] = set()
    repeated: dict[str, None] = {}
    for line in lines:
        if line.lstrip().startswith("[+]"):
            seen = set()
        elif "integrated" in line:
            if line.strip() in seen and line.strip() not in consecutive:
                repeated[line.strip()] = None
            seen.add(line.strip())
    found.extend(Violation("I9", f"duplicate 'integrated' line: {line!r}") for line in repeated)
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if "Traceback (most recent call last)" in text:
            found.append(Violation("I9", f"{stream} has a Python traceback"))
    return found


_CHECKS: dict[str, _Check] = {
    "I1": _i1_lock_deployed,
    "I2": _i2_idempotence,
    "I3": _i3_frozen_audit,
    "I4": _i4_gate_monotonicity,
    "I5": _i5_write_set,
    "I6": _i6_ascii,
    "I7": _i7_failure_preserves_state,
    "I8": _i8_exit_code,
    "I9": _i9_no_noise,
}
_OUTPUT_LAWS = ("I6", "I8", "I9")


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


# --- Generated obligations (security tables and journey step oracles) -------
#
# ``check_obligation`` is the one entry point that credits a generated
# obligation id (``tests/lifecycle/obligation_space.py``). The caller passes
# the facts to check; the oracle checks them itself against the snapshots,
# the egress log and the command result, and writes the receipt only when
# every fact holds. Required strengths: ``unit`` needs an owner-level value
# comparison; ``e2e`` needs a CLI result; ``e2e-evidence`` also needs a state
# fact (snapshot paths, unchanged bytes, egress log or persisted-secret scan).

OBLIGATION_ORACLE = "OBLIGATION"
_UNSET = object()


@dataclass(frozen=True)
class Evidence:
    """Facts ``check_obligation`` verifies for one generated obligation."""

    # Root id -> relative paths that must be absent / present after the step.
    absent: Mapping[str, Collection[str]] = field(default_factory=dict)
    present: Mapping[str, Collection[str]] = field(default_factory=dict)
    # Root id -> path prefixes whose entries must be byte-identical before and after.
    unchanged: Mapping[str, Collection[str]] = field(default_factory=dict)
    # Egress log entries of the step (dicts with at least ``host``).
    egress: tuple[Mapping[str, object], ...] | None = None
    egress_absent_hosts: Collection[str] = ()
    egress_present_hosts: Collection[str] = ()
    # Values that must appear in no file under the after roots and in no output.
    secrets_absent: Collection[str] = ()
    exit: int | Collection[int] | None = None
    # Regexes that must / must not match the combined output.
    output_matches: Collection[str] = ()
    output_forbids: Collection[str] = ()
    ascii_output: bool = False
    # Owner-level value comparison (``unit`` strength, golden transcripts).
    expected: object = _UNSET
    observed: object = _UNSET

    def state_facts(self) -> bool:
        return bool(
            self.absent
            or self.present
            or self.unchanged
            or self.egress is not None
            or self.secrets_absent
        )


def _artifacts(value: Observation | ArtifactSnapshotSet | None) -> ArtifactSnapshotSet | None:
    return value.artifacts if isinstance(value, Observation) else value


def check_obligation(
    cell_id: str,
    *,
    strength: str,
    before: Observation | ArtifactSnapshotSet | None = None,
    after: Observation | ArtifactSnapshotSet | None = None,
    result: CommandResult | None = None,
    evidence: Evidence,
    scope: str = "project",
) -> None:
    """Check *evidence* for generated obligation *cell_id*; emit its receipt if it holds."""
    problems = _strength_problems(strength, before, after, result, evidence)
    problems += _evidence_problems(_artifacts(before), _artifacts(after), result, evidence)
    assert not problems, f"{cell_id}:\n" + "\n".join(problems) + _evidence(result)
    pre, post = _artifacts(before), _artifacts(after)
    receipts.emit(
        receipts.Receipt(
            node=receipts.current_node(),
            params={"obligation": cell_id, "strength": strength},
            pre_digest=_artifact_digest(pre),
            argv=tuple(result.command) if result is not None else (),
            scope=scope,
            post_digest=_artifact_digest(post),
            oracle_id=OBLIGATION_ORACLE,
            outcome="refusal" if result is not None and result.returncode != 0 else "success",
            lane=receipts.current_lane(),
        )
    )


def _strength_problems(strength, before, after, result, evidence: Evidence) -> list[str]:
    if strength == "unit":
        if evidence.expected is _UNSET or evidence.observed is _UNSET:
            return ["unit strength needs an expected/observed owner value"]
        return []
    if strength not in ("e2e", "e2e-evidence"):
        return [f"unknown strength {strength!r}"]
    problems = [] if result is not None else [f"{strength} strength needs a CLI result"]
    if strength == "e2e-evidence":
        if not evidence.state_facts():
            problems.append("e2e-evidence needs a snapshot, egress or persisted-secret fact")
        needs_pair = evidence.absent or evidence.present or evidence.unchanged
        if needs_pair and (before is None or after is None):
            problems.append("snapshot facts need both before and after")
    return problems


def _evidence_problems(
    before: ArtifactSnapshotSet | None,
    after: ArtifactSnapshotSet | None,
    result: CommandResult | None,
    evidence: Evidence,
) -> list[str]:
    problems: list[str] = []
    for root_id, paths in evidence.absent.items():
        present = sorted(set(paths) & after.snapshot(root_id).paths) if after else []
        problems += [f"{root_id}:{path} must be absent" for path in present]
    for root_id, paths in evidence.present.items():
        missing = sorted(set(paths) - after.snapshot(root_id).paths) if after else sorted(paths)
        problems += [f"{root_id}:{path} must be present" for path in missing]
    for root_id, prefixes in evidence.unchanged.items():
        if before is None or after is None:
            break
        difference = before.snapshot(root_id).diff(after.snapshot(root_id))
        moved = difference.added | difference.removed | difference.changed
        problems += [
            f"{root_id}:{path} changed"
            for path in sorted(moved)
            if any(_within(path, prefix) for prefix in prefixes)
        ]
    if evidence.egress is not None:
        hosts = {str(entry.get("host", "")) for entry in evidence.egress}
        problems += [
            f"egress reached {host}" for host in sorted(hosts & set(evidence.egress_absent_hosts))
        ]
        problems += [
            f"egress never reached {host}"
            for host in sorted(set(evidence.egress_present_hosts) - hosts)
        ]
    output = "" if result is None else result.stdout + result.stderr
    problems += _secret_problems(after, output, evidence.secrets_absent)
    if evidence.exit is not None and result is not None:
        wanted = {evidence.exit} if isinstance(evidence.exit, int) else set(evidence.exit)
        if result.returncode not in wanted:
            problems.append(f"exit {result.returncode}, wanted {sorted(wanted)}")
    problems += [
        f"output lacks /{rx}/" for rx in evidence.output_matches if not re.search(rx, output, re.M)
    ]
    problems += [
        f"output has /{rx}/" for rx in evidence.output_forbids if re.search(rx, output, re.M)
    ]
    if evidence.ascii_output and not output.isascii():
        problems.append("output is not ASCII")
    if evidence.expected is not _UNSET and evidence.expected != evidence.observed:
        problems.append(f"observed {evidence.observed!r}, expected {evidence.expected!r}")
    return problems


def _secret_problems(
    after: ArtifactSnapshotSet | None, output: str, secrets: Collection[str]
) -> list[str]:
    if not secrets:
        return []
    problems = [
        f"output carries secret #{index}" for index, value in enumerate(secrets) if value in output
    ]
    for root_id, snapshot in after.snapshots if after else ():
        for path in sorted(snapshot.paths):
            file = snapshot.root / path
            if not file.is_file():
                continue
            data = file.read_bytes()
            problems += [
                f"{root_id}:{path} carries secret #{index}"
                for index, value in enumerate(secrets)
                if value.encode() in data
            ]
    return problems


def _artifact_digest(artifacts: ArtifactSnapshotSet | None) -> str:
    if artifacts is None:
        return ""
    digest = hashlib.sha256()
    for root_id, snapshot in artifacts.snapshots:
        for entry in snapshot.entries:
            digest.update(f"{root_id}:{entry.relative_path}:{entry.fingerprint}\n".encode())
    return "sha256:" + digest.hexdigest()
