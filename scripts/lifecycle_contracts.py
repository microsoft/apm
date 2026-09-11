"""Authored lifecycle obligations in the existing escaped-defect ledger.

Semantic applicability is reviewed, not inferred from filenames or test names.
This module owns mechanical validation; pytest owns executable evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

LEDGER = "tests/fixtures/lifecycle_bug_ledger.json"


class EvidenceError(ValueError):
    """A candidate has not discharged its lifecycle obligations."""


def git(root: Path, *args: str) -> str:
    """Run a read-only git query, preserving command failures."""
    executable = shutil.which("git")
    if executable is None:
        raise EvidenceError("git is required for candidate identity")
    result = subprocess.check_output(  # noqa: S603 - fixed executable, argument vector, no shell
        [executable, *args], cwd=root, text=True
    )
    return result if "-z" in args else result.strip()


def command_inventory() -> dict[str, click.Command]:
    """Walk actual Click registrations, including groups, defaults and aliases."""
    from apm_cli.cli import cli

    result: dict[str, click.Command] = {}

    def walk(command: click.Command, path: str, parent: click.Context | None) -> None:
        result[path] = command
        if isinstance(command, click.Group):
            with click.Context(command, parent=parent, info_name=path) as context:
                for name in command.list_commands(context):
                    child = command.get_command(context, name)
                    if child is None:
                        raise EvidenceError(f"Unresolved Click registration: {path} {name}")
                    walk(child, f"{path} {name}".strip(), context)

    walk(cli, "", None)
    return result


def command_path(args: list[str], inventory: dict[str, click.Command]) -> str:
    """Resolve command tokens with Click's parsers, not a remembered argv list."""
    path = ""
    remaining = args[:]
    parent = None
    while True:
        command = inventory[path]
        context = command.make_context(path, remaining, parent=parent, resilient_parsing=True)
        if not isinstance(command, click.Group):
            return path
        # Click keeps the first subcommand separately from its remaining args.
        remaining = [*context._protected_args, *context.args]
        if not remaining:
            return path
        candidate = f"{path} {remaining[0]}".strip()
        if candidate not in inventory:
            return path
        path, remaining, parent = candidate, remaining[1:], context


def load_ledger(text: str) -> dict[str, Any]:
    """Decode the canonical ledger, including additive general contracts."""
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise EvidenceError("Unsupported lifecycle ledger")
    return value


def _require(condition: object, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _paths(value: object) -> list[str]:
    _require(isinstance(value, list) and value, "Expected nonempty exact paths")
    for path in value:
        _require(
            isinstance(path, str)
            and path
            and not path.startswith("/")
            and "\\" not in path
            and all(part not in ("", ".", "..") for part in path.split("/"))
            and not any(char in path for char in "*?["),
            f"Not an exact repository path: {path}",
        )
    return value


def validate_contracts(
    ledger: dict[str, Any], inventory: dict[str, click.Command]
) -> dict[str, dict[str, Any]]:
    """Validate complete applicability and exact witness/dimension obligations."""
    properties = {row["id"] for row in ledger["property_catalog"]}
    contracts: dict[str, dict[str, Any]] = {}
    for row in ledger.get("lifecycle_contracts", []):
        identifier = row["id"]
        _require(identifier and identifier not in contracts, "Duplicate/empty contract ID")
        _require(row.get("assessment"), f"{identifier}: missing reviewed assessment")
        _paths(row["paths"])
        _require(
            row["properties"] and set(row["properties"]) <= properties,
            f"{identifier}: unknown/missing properties",
        )
        _require(set(row["commands"]) == set(inventory), f"{identifier}: incomplete command map")
        witnesses = {w["id"]: w for w in row["witnesses"]}
        _require(
            len(witnesses) == len(row["witnesses"]) and witnesses,
            f"{identifier}: duplicate/empty witnesses",
        )
        _require(
            {w["kind"] for w in witnesses.values()} == {"deterministic", "generated"},
            f"{identifier}: both deterministic and generated witnesses required",
        )
        for witness in witnesses.values():
            nodeid = witness["nodeid"]
            _require(
                isinstance(nodeid, str) and "::" in nodeid and nodeid.startswith("tests/"),
                f"{identifier}: invalid exact nodeid",
            )
            _paths([nodeid.split("::")[0]])
            _require(len(witness["transitions"]) >= 2, f"{nodeid}: missing trajectory")
            for transition in witness["transitions"]:
                _require(
                    transition.get("context", "initial") in {"initial", "APM_HOME"},
                    f"{nodeid}: unsupported command context",
                )
                _require(transition["command"] in inventory, f"{nodeid}: unknown command")
                _require(type(transition["returncode"]) is int, f"{nodeid}: invalid exit status")
                _require(
                    transition["state"] in {"observed", "unchanged", "changed"},
                    f"{nodeid}: missing state observation",
                )
                _require(
                    isinstance(transition["argv_contains"], list)
                    and all(isinstance(arg, str) for arg in transition["argv_contains"]),
                    f"{nodeid}: invalid argv constraints",
                )
                if "preparation" in transition:
                    _require(
                        isinstance(transition["preparation"], str)
                        and transition["preparation"].strip(),
                        f"{nodeid}: preparation needs a reviewed mutation rationale",
                    )
        for param, values in row["dimensions"].items():
            _require(values and isinstance(values, list), f"{identifier}: empty dimension")
            for kind in ("deterministic", "generated"):
                observed = {
                    w["dimensions"].get(param) for w in witnesses.values() if w["kind"] == kind
                }
                _require(
                    set(values) <= observed,
                    f"{identifier}: {kind} missing required dimension {param}",
                )
        applicable = False
        for command, entry in row["commands"].items():
            _require(entry["reason"].strip(), f"{identifier}: missing command rationale")
            disposition = entry["disposition"]
            _require(disposition in {"applicable", "semantic_na"}, "Unknown applicability")
            if disposition == "semantic_na":
                continue
            applicable = True
            for kind in ("deterministic", "generated"):
                refs = entry[kind]
                _require(refs, f"{identifier}: {command} missing {kind} witness")
                for ref in refs:
                    _require(
                        ref in witnesses and witnesses[ref]["kind"] == kind,
                        f"{identifier}: invalid witness reference {ref}",
                    )
                    _require(
                        any(t["command"] == command for t in witnesses[ref]["transitions"]),
                        f"{identifier}: {command} not exercised by {ref}",
                    )
        _require(applicable, f"{identifier}: no applicable commands")
        contracts[identifier] = row
    return contracts


def runtime_candidate(path: str) -> bool:
    """Only known repository-only surfaces are automatically non-runtime.

    Unknown roots/extensions, packages, templates, locks and manifests default
    to runtime. Renames are processed as deletion plus addition.
    """
    return not (
        path.startswith(("tests/", "docs/", ".github/workflows/"))
        or path == ".github/CODEOWNERS"
        or ("/" not in path and path.endswith(".md"))
    )


def select_contracts(
    base: dict[str, Any],
    head: dict[str, Any],
    changed: set[str],
    inventory: dict[str, click.Command],
) -> list[dict[str, Any]]:
    """Fail closed on uncovered runtime changes or deleted/weakened obligations."""
    current = validate_contracts(head, inventory)
    for row in base["property_catalog"]:
        _require(row in head["property_catalog"], f"Removed/changed property: {row['id']}")
    bugs = {row["issue"]: row for row in head.get("bugs", [])}
    for row in base.get("bugs", []):
        replacement = bugs.get(row["issue"], {})
        _require(
            all(
                set(row[key]) <= set(replacement.get(key, []))
                for key in ("properties", "regression_tests")
            ),
            f"Removed legacy regression obligation: {row['issue']}",
        )
    previous = {row["id"]: row for row in base.get("lifecycle_contracts", [])}
    selected = set()
    for identifier, old in previous.items():
        _require(identifier in current, f"Removed lifecycle contract: {identifier}")
        new = current[identifier]
        for key in ("paths", "properties"):
            _require(set(old[key]) <= set(new[key]), f"Weakened {identifier}: {key}")
        for witness in old["witnesses"]:
            _require(witness in new["witnesses"], f"Removed/changed witness: {identifier}")
        for name, values in old["dimensions"].items():
            _require(
                set(values) <= set(new["dimensions"].get(name, [])),
                f"Removed dimension: {identifier}/{name}",
            )
        for command, entry in old["commands"].items():
            if entry["disposition"] == "applicable":
                replacement = new["commands"].get(command, {})
                _require(
                    replacement.get("disposition") == "applicable"
                    and all(
                        set(entry[kind]) <= set(replacement.get(kind, []))
                        for kind in ("deterministic", "generated")
                    ),
                    f"Removed command obligation: {identifier}/{command}",
                )
        if any(w["nodeid"].split("::")[0] in changed for w in old["witnesses"]):
            selected.add(identifier)

    excluded: set[str] = set()
    for entry in head.get("non_runtime_changes", []):
        _require(entry["reason"].strip(), "Non-runtime assessment needs a reason")
        paths = _paths(entry["paths"])
        _require(
            all(
                path.startswith(("scripts/", ".apm/", ".agents/", ".github/", "packages/"))
                for path in paths
            ),
            "Non-runtime exceptions are limited to reviewed repository tooling/agent content",
        )
        # A prior exception cannot silently exempt a later change.
        if entry not in base.get("non_runtime_changes", []):
            excluded.update(paths)
    runtime = {path for path in changed if runtime_candidate(path)}
    for path in runtime:
        matching = {
            identifier
            for identifier, row in current.items()
            if path in row["paths"] and row != previous.get(identifier)
        }
        if matching:
            selected.update(matching)
        else:
            _require(path in excluded, f"Missing fresh lifecycle assessment for {path}")
    # Contract additions/edits must execute even in an otherwise tests-only diff.
    selected.update(key for key, row in current.items() if row != previous.get(key))
    return [current[key] for key in sorted(selected)]


def candidate_contracts(
    root: Path, base: str, head: str, inventory: dict[str, click.Command]
) -> tuple[set[str], list[dict[str, Any]]]:
    """One diff-to-obligation authority for smoke deferral and native execution."""
    previous = load_ledger(git(root, "show", f"{base}:{LEDGER}"))
    current = load_ledger((root / LEDGER).read_text(encoding="utf-8"))
    changed = set(
        git(root, "diff", "--name-only", "--no-renames", "-z", base, head).split("\0")
    ) - {""}
    return changed, select_contracts(previous, current, changed, inventory)


def validate_execution(witness: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Check actual collection, phases, model execution and persistent trajectory."""
    nodeid = witness["nodeid"]
    _require(evidence.get("collected"), f"{nodeid}: not collected (exact nodeid required)")
    _require(
        evidence.get("phases") == {"setup": "passed", "call": "passed", "teardown": "passed"},
        f"{nodeid}: setup/call/teardown must pass without skip or xfail",
    )
    for name, value in witness["dimensions"].items():
        _require(
            evidence["dimensions"].get(name) == value,
            f"{nodeid}: unexecuted parameter dimension {name}={value}",
        )
    if witness["kind"] == "generated":
        _require(evidence.get("models", 0) > 0, f"{nodeid}: no executed generated model")
    events = evidence.get("events", [])
    if witness["kind"] == "generated":
        events = [
            event
            for event in events
            if event.get("model") is not None and 0 < event["model"] <= evidence["models"]
        ]

    # One trajectory cannot be assembled out of independent temporary projects.
    def trajectory(event: dict[str, Any]) -> tuple[str, int | None]:
        _require(event.get("roots"), f"{nodeid}: missing durable-root identity")
        environment = event.get("environment")
        _require(
            isinstance(environment, dict)
            and set(environment) == set(event["roots"]) - {"workspace"}
            and all(isinstance(value, str) and value for value in environment.values()),
            f"{nodeid}: missing or inconsistent environment identity",
        )
        context = event.get("context", "initial")
        _require(context in {"initial", "APM_HOME"}, f"{nodeid}: unknown observed context")
        root = "workspace" if context == "initial" else context
        _require(
            event["cwd"] == event["roots"].get(root),
            f"{nodeid}: cwd is outside its observed context",
        )
        return (
            json.dumps([event["roots"], environment], sort_keys=True),
            event.get("model"),
        )

    def matches(event: dict[str, Any], expected: dict[str, Any]) -> bool:
        return (
            event["cwd"]
            == event["roots"].get(
                "workspace" if expected.get("context", "initial") == "initial" else "APM_HOME"
            )
            and event["command"] == expected["command"]
            and event["returncode"] == expected["returncode"]
            and all(arg in event["args"] for arg in expected["argv_contains"])
            and (
                expected["state"] == "observed"
                or (event["before"] == event["after"]) == (expected["state"] == "unchanged")
            )
        )

    workspaces = {trajectory(event) for event in events}
    for workspace in workspaces:
        cursor = 0
        previous_after = None
        for event in (event for event in events if trajectory(event) == workspace):
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
