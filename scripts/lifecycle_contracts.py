"""Mechanical validation of the single authored #2867 native lifecycle contract.

Adapted from be73ed139f3a73c19437a895dfd6d68e41df8bfc. This is not a
runtime-change classifier, waiver system, or repository shipping policy.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

LEDGER = "tests/fixtures/lifecycle_bug_ledger.json"
CONTRACT_ID = "pr2876-alias-path-complete-lifecycle"
_REQUIRED = "tests/integration/test_required_lifecycle_state_machine.py"
_GENERATED = "tests/integration/test_generated_lifecycle_state_machine.py"
# Deliberately independent of editable ledger membership: deletion cannot waive proof.
REQUIRED_WITNESSES = (
    *(
        f"{_REQUIRED}::test_required_global_audit_rule_matrix_for_external_roots[{value}]"
        for value in ("False", "True")
    ),
    *(
        f"{_GENERATED}::{name}[{variant}]"
        for name in (
            "test_generated_lifecycle_mandatory_replay",
            "test_generated_lifecycle_sequences_preserve_reference_model",
        )
        for variant in ("project", "global-canonical", "global-aliased")
    ),
    f"{_REQUIRED}::test_required_frozen_semver_transport_refusal_recovers",
)


class EvidenceError(ValueError):
    """A candidate has not discharged its lifecycle obligations."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def git(root: Path, *args: str) -> str:
    """Run a read-only Git query without replacement-object interpretation."""
    executable = shutil.which("git")
    _require(executable, "git is required for candidate identity")
    result = subprocess.check_output(  # noqa: S603 - resolved executable, argument vector, no shell
        [executable, "--no-replace-objects", *args], cwd=root, text=True
    )
    return result if "-z" in args else result.strip()


def command_inventory() -> dict[str, click.Command]:
    """Walk actual Click registrations, including hidden names and aliases."""
    from apm_cli.cli import cli

    result: dict[str, click.Command] = {}

    def walk(command: click.Command, path: str, parent: click.Context | None) -> None:
        result[path] = command
        if isinstance(command, click.Group):
            with click.Context(command, parent=parent, info_name=path) as context:
                for name in command.list_commands(context):
                    child = command.get_command(context, name)
                    _require(child is not None, f"Unresolved Click registration: {path} {name}")
                    walk(child, f"{path} {name}".strip(), context)

    walk(cli, "", None)
    return result


def command_path(args: list[str], inventory: dict[str, click.Command]) -> str:
    """Resolve real command tokens using Click rather than argv substring guesses."""
    path, remaining, parent = "", args[:], None
    while True:
        command = inventory[path]
        context = command.make_context(path, remaining, parent=parent, resilient_parsing=True)
        if not isinstance(command, click.Group):
            return path
        remaining = [*context._protected_args, *context.args]
        if not remaining:
            return path
        candidate = f"{path} {remaining[0]}".strip()
        if candidate not in inventory:
            return path
        path, remaining, parent = candidate, remaining[1:], context


def load_ledger(text: str) -> dict[str, Any]:
    """Decode the additive ledger, expanding authored shared trajectory segments."""
    value = json.loads(text)
    _require(isinstance(value, dict) and value.get("schema_version") == 1, "Unsupported ledger")
    for contract in value.get("lifecycle_contracts", []):
        trajectories = contract.get("trajectories", {})
        for witness in contract["witnesses"]:
            witness["transitions"] = [
                copy.deepcopy(transition)
                for segment in witness["trajectory"]
                for transition in trajectories[segment]
            ]
    return value


def validate_contracts(
    ledger: dict[str, Any], inventory: dict[str, click.Command]
) -> dict[str, dict[str, Any]]:
    """Require the bounded contract, exact witnesses and complete resource assessment."""
    rows = ledger.get("lifecycle_contracts", [])
    _require(len(rows) == 1 and rows[0]["id"] == CONTRACT_ID, "Exactly one #2867 contract required")
    row = rows[0]
    _require(row.get("assessment"), "Missing reviewed assessment")
    properties = {item["id"] for item in ledger["property_catalog"]}
    _require(row["properties"] and set(row["properties"]) <= properties, "Unknown properties")
    _require(set(row["commands"]) == set(inventory), "Incomplete current command inventory")
    witnesses = {item["id"]: item for item in row["witnesses"]}
    _require(len(witnesses) == len(REQUIRED_WITNESSES), "Duplicate/missing witnesses")
    _require(
        {w["nodeid"] for w in witnesses.values()} == set(REQUIRED_WITNESSES),
        "Exact immutable witness identities required",
    )
    for witness in witnesses.values():
        nodeid = witness["nodeid"]
        generated = "::test_generated_lifecycle_sequences_" in nodeid
        _require(
            witness["kind"] == ("generated" if generated else "deterministic"),
            f"{nodeid}: wrong execution kind",
        )
        if nodeid.startswith(_GENERATED):
            dimensions = {"variant": nodeid.rsplit("[", 1)[1][:-1]}
        elif nodeid.endswith(("[False]", "[True]")):
            dimensions = {"aliased_home": nodeid.endswith("[True]")}
        else:
            dimensions = {}
        _require(
            json.dumps(witness["dimensions"], sort_keys=True)
            == json.dumps(dimensions, sort_keys=True),
            f"{nodeid}: changed dimensions",
        )
        _require(len(witness["transitions"]) >= 2, f"{nodeid}: missing trajectory")
        for transition in witness["transitions"]:
            _require(transition["command"] in inventory, f"{nodeid}: unknown command")
            _require(type(transition["returncode"]) is int, f"{nodeid}: invalid outcome")
            _require(
                transition["state"] in {"observed", "changed", "unchanged"},
                f"{nodeid}: invalid state",
            )
            _require(
                transition.get("context", "initial") in {"initial", "APM_HOME"},
                f"{nodeid}: invalid context",
            )
            _require(
                isinstance(transition["argv_contains"], list)
                and all(isinstance(arg, str) for arg in transition["argv_contains"]),
                f"{nodeid}: invalid command arguments",
            )
            if "preparation" in transition:
                _require(
                    isinstance(transition["preparation"], str)
                    and transition["preparation"].strip(),
                    f"{nodeid}: preparation needs an authored rationale",
                )
    for command, entry in row["commands"].items():
        _require(isinstance(entry["reason"], str) and entry["reason"].strip(), "Missing reason")
        _require(entry["disposition"] in {"applicable", "semantic_na"}, "Unknown applicability")
        if entry["disposition"] == "semantic_na":
            continue
        for kind in ("deterministic", "generated"):
            refs = entry[kind]
            _require(refs, f"{command}: missing {kind} witness")
            for ref in refs:
                _require(
                    ref in witnesses and witnesses[ref]["kind"] == kind,
                    f"{command}: invalid witness reference",
                )
                _require(
                    any(t["command"] == command for t in witnesses[ref]["transitions"]),
                    f"{command}: not exercised by {ref}",
                )
    exercised = {t["command"] for w in witnesses.values() for t in w["transitions"]}
    _require(
        exercised == {c for c, e in row["commands"].items() if e["disposition"] == "applicable"},
        "Command applicability must match actual authored transitions",
    )
    return {CONTRACT_ID: row}


def candidate_contract(
    root: Path, base: str, inventory: dict[str, click.Command]
) -> dict[str, Any]:
    """Always select #2867; preserve legacy property, bug and known-gap records."""
    previous = json.loads(git(root, "show", f"{base}:{LEDGER}"))
    current = load_ledger((root / LEDGER).read_text(encoding="utf-8"))
    for field in ("property_catalog", "bugs", "known_gaps"):
        for row in previous.get(field, []):
            _require(row in current[field], f"Removed/changed legacy {field} record")
    return validate_contracts(current, inventory)[CONTRACT_ID]


def validate_execution(witness: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Validate actual phases and a connected, ordered command/root/state trajectory."""
    nodeid = witness["nodeid"]
    _require(evidence.get("collected") is True, f"{nodeid}: not collected")
    _require(
        evidence.get("phases") == {"setup": "passed", "call": "passed", "teardown": "passed"},
        f"{nodeid}: setup/call/teardown must pass without skip or xfail",
    )
    _require(
        json.dumps(evidence.get("dimensions"), sort_keys=True)
        == json.dumps(witness["dimensions"], sort_keys=True),
        f"{nodeid}: wrong dimensions",
    )
    generated = witness["kind"] == "generated"
    if generated:
        _require(
            type(evidence.get("models")) is int and evidence["models"] > 0,
            f"{nodeid}: no executed generated model",
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in evidence.get("events", []):
        roots, environment = event.get("roots"), event.get("environment")
        _require(
            isinstance(roots, dict)
            and roots.get("workspace")
            and isinstance(environment, dict)
            and environment
            and set(environment) == set(roots) - {"workspace"}
            and all(isinstance(v, str) and v for v in [*roots.values(), *environment.values()]),
            f"{nodeid}: missing durable-root/environment identity",
        )
        context = event.get("context", "initial")
        _require(context in {"initial", "APM_HOME"}, f"{nodeid}: unknown context")
        _require(
            event["cwd"] == roots.get("workspace" if context == "initial" else context),
            f"{nodeid}: cwd outside observed context",
        )
        _require(
            all(isinstance(event.get(k), str) and event[k] for k in ("before", "after")),
            f"{nodeid}: missing state observation",
        )
        if generated and not (
            type(event.get("model")) is int and 0 < event["model"] <= evidence["models"]
        ):
            continue
        identity = json.dumps([roots, environment, event.get("model")], sort_keys=True)
        grouped.setdefault(identity, []).append(event)

    def matches(event: dict[str, Any], expected: dict[str, Any]) -> bool:
        return (
            event.get("context", "initial") == expected.get("context", "initial")
            and event["command"] == expected["command"]
            and type(event["returncode"]) is int
            and event["returncode"] == expected["returncode"]
            and all(arg in event["args"] for arg in expected["argv_contains"])
            and (
                expected["state"] == "observed"
                or (event["before"] == event["after"]) == (expected["state"] == "unchanged")
            )
        )

    for events in grouped.values():
        cursor, previous_after = 0, None
        for event in events:
            expected = witness["transitions"][cursor]
            if (
                previous_after is not None
                and previous_after != event["before"]
                and not (expected.get("preparation") and matches(event, expected))
            ):
                cursor = 0
                expected = witness["transitions"][cursor]
            previous_after = event["after"]
            if matches(event, expected):
                cursor += 1
                if cursor == len(witness["transitions"]):
                    return
    raise EvidenceError(f"{nodeid}: required ordered command/state trajectory not executed")
