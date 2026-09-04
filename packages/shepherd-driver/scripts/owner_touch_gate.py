#!/usr/bin/env python3
"""Detect canonical-owner touches and verify shepherd completion evidence."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, overload

TABLE_START = "<!-- canonical-owner-table:v1 -->"
TABLE_END = "<!-- /canonical-owner-table -->"
TABLE_HEADER = ("Decision / fact", "Canonical owner", "Owner path selectors")
REPORT_VERSION = "1"
EVIDENCE_VERSION = "2"
DEFAULT_OWNER_TABLE = ".apm/instructions/architecture.instructions.md"
REGISTRY_ROOT = ".apm/architecture/owners"
REGISTRY_INDEX = f"{REGISTRY_ROOT}/index.json"
REGISTRY_VERSION = 1
_INDEX_FIELDS = frozenset({"version", "shards"})
_SHARD_FIELDS = frozenset({"version", "owners"})
_OWNER_FIELDS = frozenset({"id", "decision", "owner", "selectors", "guards"})
_KEBAB_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHARD_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.json")
OWNER_CLASSIFICATIONS = {
    "owner-extension",
    "new-owner",
    "split-authority-repair",
}

# Cached, PATH-resolved git executable. This script ships as a
# standalone, portable APM skill artifact (see packages/shepherd-driver)
# and must not depend on the main repo's apm_cli package being
# importable, so it resolves git itself rather than reusing
# apm_cli.utils.git_env.get_git_executable(). A bare "git" argv does
# not reliably resolve on Windows (CreateProcess with shell=False does
# not perform a PATH search for extension-less names), which raised
# FileNotFoundError: [WinError 2] (see microsoft/apm#2233).
_GIT_EXECUTABLE: str | None = None


def _git_executable() -> str:
    """Return the cached, fully-resolved path to the git executable."""
    global _GIT_EXECUTABLE
    if _GIT_EXECUTABLE is None:
        resolved = shutil.which("git")
        if resolved is None:
            raise GateError("git executable not found on PATH")
        _GIT_EXECUTABLE = resolved
    return _GIT_EXECUTABLE


class GateError(RuntimeError):
    """Raised when owner detection or evidence verification must fail closed."""


@dataclass(frozen=True)
class OwnerRow:
    """One parsed row from the canonical architecture owner table."""

    id: str
    decision: str
    owner: str
    selectors: tuple[str, ...]
    guards: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()


@overload
def _git(repo_root: Path, *args: str, text: Literal[True] = True) -> str: ...


@overload
def _git(repo_root: Path, *args: str, text: Literal[False]) -> bytes: ...


def _git(repo_root: Path, *args: str, text: bool = True) -> str | bytes:
    """Run git in repo_root and return stdout, raising GateError on failure."""
    # Every operation in this gate resolves or reads an exact revision. Git
    # replacement refs are local, mutable indirections: without this option a
    # refs/replace entry can make ``show``/``diff``/``ls-tree`` inspect
    # substituted content while ``rev-parse`` and the report still name the
    # original commit SHA. Keep the hardening on the common standalone command
    # path so no exact-revision operation can accidentally omit it.
    command = [_git_executable(), "--no-replace-objects", "-C", str(repo_root), *args]
    completed = subprocess.run(  # noqa: S603 - fixed, PATH-resolved git executable, no shell
        command,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", errors="replace")
        raise GateError(f"{' '.join(command)} failed: {stderr.strip()}")
    return completed.stdout


def _resolve_revision(repo_root: Path, revision: str) -> str:
    """Resolve revision to a full commit SHA."""
    resolved = _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return resolved.strip()


def _file_at_revision(
    repo_root: Path,
    revision_sha: str,
    path: str,
) -> bytes:
    """Read one file from an exact commit."""
    return _git(repo_root, "show", f"{revision_sha}:{path}", text=False)


def _split_markdown_row(line: str) -> tuple[str, ...]:
    """Split a simple Markdown table row into stripped cells."""
    if not line.startswith("|") or not line.endswith("|"):
        raise GateError(f"malformed owner-table row: {line!r}")
    return tuple(cell.strip() for cell in line[1:-1].split("|"))


def _validate_selector(selector: str) -> None:
    """Reject selectors that are unsafe, ambiguous, or non-portable."""
    if not selector or not selector.isascii() or any(ord(char) < 32 for char in selector):
        raise GateError(f"invalid owner path selector: {selector!r}")
    path = PurePosixPath(selector)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or selector.startswith("./")
        or "//" in selector
    ):
        raise GateError(f"owner path selector must be repository-relative: {selector!r}")
    if selector.endswith("/") or "\\" in selector:
        raise GateError(f"owner path selector must use a file-oriented POSIX pattern: {selector!r}")


def parse_owner_table(
    content: bytes,
    *,
    source_path: str = DEFAULT_OWNER_TABLE,
) -> list[OwnerRow]:
    """Parse the one canonical owner table, rejecting any structural drift."""
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateError("canonical owner table must be printable ASCII") from exc

    lines = text.splitlines()
    if lines.count(TABLE_START) != 1 or lines.count(TABLE_END) != 1:
        raise GateError("canonical owner table markers are missing or duplicated")
    start = lines.index(TABLE_START)
    end = lines.index(TABLE_END)
    if end <= start + 2:
        raise GateError("canonical owner table is empty")

    table_lines = [line.strip() for line in lines[start + 1 : end] if line.strip()]
    if len(table_lines) < 3:
        raise GateError("canonical owner table has no data rows")
    if _split_markdown_row(table_lines[0]) != TABLE_HEADER:
        raise GateError("canonical owner table header drifted")

    delimiter = _split_markdown_row(table_lines[1])
    if len(delimiter) != len(TABLE_HEADER) or any(
        re.fullmatch(r":?-{3,}:?", cell) is None for cell in delimiter
    ):
        raise GateError("canonical owner table delimiter drifted")

    rows: list[OwnerRow] = []
    decisions: set[str] = set()
    selectors_seen: set[str] = set()
    for line in table_lines[2:]:
        cells = _split_markdown_row(line)
        if len(cells) != len(TABLE_HEADER):
            raise GateError(f"canonical owner table row has {len(cells)} cells, expected 3")
        decision, owner, selector_cell = cells
        if not decision or not owner:
            raise GateError("canonical owner table decision and owner must be non-empty")
        if decision in decisions:
            raise GateError(f"duplicate canonical owner decision: {decision}")

        selector_parts = selector_cell.split(";")
        if any(not part.strip() for part in selector_parts):
            raise GateError(f"canonical owner row has no selectors: {decision}")
        selectors_list: list[str] = []
        for part in selector_parts:
            selector = part.strip()
            if selector.startswith("`") or selector.endswith("`"):
                if not (selector.startswith("`") and selector.endswith("`") and len(selector) > 2):
                    raise GateError(f"malformed owner path selector: {selector!r}")
                selector = selector[1:-1]
            if "`" in selector:
                raise GateError(f"malformed owner path selector: {selector!r}")
            selectors_list.append(selector)
        selectors = tuple(selectors_list)
        for selector in selectors:
            _validate_selector(selector)
            if selector in selectors_seen:
                raise GateError(f"duplicate canonical owner selector: {selector}")
            selectors_seen.add(selector)

        decisions.add(decision)
        legacy_id = f"legacy-{hashlib.sha256(decision.encode('ascii')).hexdigest()}"
        rows.append(
            OwnerRow(
                id=legacy_id,
                decision=decision,
                owner=owner,
                selectors=selectors,
                source_paths=(source_path,),
            )
        )

    return rows


def _reject_duplicate_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate field names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_registry_json(content: bytes, label: str) -> Any:
    """Decode one strict printable-ASCII registry document."""
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateError(f"{label} must be printable ASCII") from exc
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise GateError(f"{label} must be printable ASCII")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_fields)
    except json.JSONDecodeError as exc:
        raise GateError(f"{label} is malformed JSON: {exc.msg}") from exc


def _registry_mapping(value: Any, label: str) -> dict[str, Any]:
    """Require one registry JSON object."""
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return value


def _registry_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    """Reject unknown and missing registry fields."""
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise GateError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise GateError(f"{label} is missing fields: {', '.join(missing)}")


def _registry_version(value: Any, label: str) -> None:
    """Require the exact supported registry version."""
    if type(value) is not int or value != REGISTRY_VERSION:
        raise GateError(f"{label}.version must be {REGISTRY_VERSION}")


def _registry_string(value: Any, label: str) -> str:
    """Require one non-empty trimmed printable-ASCII string."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise GateError(f"{label} must be a non-empty trimmed string")
    if not value.isascii() or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise GateError(f"{label} must be printable ASCII")
    return value


def _registry_string_array(value: Any, label: str) -> tuple[str, ...]:
    """Require one non-empty duplicate-free registry string array."""
    if not isinstance(value, list) or not value:
        raise GateError(f"{label} must be a non-empty array")
    items = tuple(_registry_string(item, f"{label}[]") for item in value)
    if len(items) != len(set(items)):
        raise GateError(f"{label} contains duplicates")
    return items


def _semantic_owner_hash(rows: list[OwnerRow]) -> str:
    """Hash normalized ownership semantics, independent of formatting and order."""
    owners = [
        {
            "id": row.id,
            "decision": row.decision,
            "owner": row.owner,
            "selectors": sorted(row.selectors),
            "guards": sorted(row.guards),
        }
        for row in sorted(rows, key=lambda item: item.id)
    ]
    normalized = {"version": REGISTRY_VERSION, "owners": owners}
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _parse_owner_registry(
    index_content: bytes,
    shard_contents: dict[str, bytes],
    tracked_files: list[str],
) -> tuple[list[OwnerRow], str]:
    """Parse and validate a complete exact-revision owner registry."""
    index = _registry_mapping(_load_registry_json(index_content, "index.json"), "index.json")
    _registry_exact_fields(index, _INDEX_FIELDS, "index.json")
    _registry_version(index.get("version"), "index.json")
    shards = _registry_string_array(index.get("shards"), "index.json.shards")
    for shard in shards:
        if _SHARD_NAME.fullmatch(shard) is None or shard == "index.json":
            raise GateError(f"invalid registry shard name: {shard!r}")

    listed = set(shards)
    supplied = set(shard_contents)
    missing = sorted(listed - supplied)
    unlisted = sorted(supplied - listed)
    if missing:
        raise GateError(f"missing registry shards: {', '.join(missing)}")
    if unlisted:
        raise GateError(f"unlisted registry shards: {', '.join(unlisted)}")

    rows: list[OwnerRow] = []
    ids: set[str] = set()
    decisions: set[str] = set()
    selectors: set[str] = set()
    guard_owners: dict[str, str] = {}
    for shard in shards:
        document = _registry_mapping(
            _load_registry_json(shard_contents[shard], shard),
            shard,
        )
        _registry_exact_fields(document, _SHARD_FIELDS, shard)
        _registry_version(document.get("version"), shard)
        raw_owners = document.get("owners")
        if not isinstance(raw_owners, list) or not raw_owners:
            raise GateError(f"{shard}.owners must be a non-empty array")
        for index_number, raw_owner in enumerate(raw_owners):
            label = f"{shard}.owners[{index_number}]"
            item = _registry_mapping(raw_owner, label)
            _registry_exact_fields(item, _OWNER_FIELDS, label)
            owner_id = _registry_string(item.get("id"), f"{label}.id")
            if _KEBAB_ID.fullmatch(owner_id) is None:
                raise GateError(f"{label}.id must be a stable kebab-case ID")
            decision = _registry_string(item.get("decision"), f"{label}.decision")
            owner = _registry_string(item.get("owner"), f"{label}.owner")
            row_selectors = _registry_string_array(
                item.get("selectors"),
                f"{label}.selectors",
            )
            guards = _registry_string_array(item.get("guards"), f"{label}.guards")
            for guard in guards:
                if _KEBAB_ID.fullmatch(guard) is None:
                    raise GateError(f"{label}.guards[] must be a stable kebab-case ID")
                assigned_owner = guard_owners.get(guard)
                if assigned_owner is not None and assigned_owner != owner_id:
                    raise GateError(
                        f"guard ID assigned to multiple owners: {guard} "
                        f"({assigned_owner}, {owner_id})"
                    )
                guard_owners[guard] = owner_id
            if owner_id in ids:
                raise GateError(f"duplicate canonical owner ID: {owner_id}")
            if decision in decisions:
                raise GateError(f"duplicate canonical owner decision: {decision}")
            for selector in row_selectors:
                _validate_selector(selector)
                if selector in selectors:
                    raise GateError(f"duplicate canonical owner selector: {selector}")
                selectors.add(selector)
            ids.add(owner_id)
            decisions.add(decision)
            rows.append(
                OwnerRow(
                    id=owner_id,
                    decision=decision,
                    owner=owner,
                    selectors=row_selectors,
                    guards=guards,
                    source_paths=(f"{REGISTRY_ROOT}/{shard}",),
                )
            )

    _validate_selector_matches(rows, tracked_files, source="registry")
    rows.sort(key=lambda item: item.id)
    return rows, _semantic_owner_hash(rows)


def _ownership_at_revision(
    repo_root: Path,
    revision_sha: str,
    owner_table: str,
    tracked_files: list[str],
) -> tuple[list[OwnerRow], str, bool]:
    """Load JSON ownership when present, otherwise exact-revision legacy Markdown."""
    prefix = f"{REGISTRY_ROOT}/"
    registry_artifacts = [path for path in tracked_files if path.startswith(prefix)]
    if REGISTRY_INDEX not in tracked_files:
        if registry_artifacts:
            raise GateError(
                "canonical owner registry artifacts exist without "
                f"{REGISTRY_INDEX}: {', '.join(registry_artifacts)}"
            )
        table_content = _file_at_revision(repo_root, revision_sha, owner_table)
        rows = parse_owner_table(table_content, source_path=owner_table)
        _validate_selector_matches(rows, tracked_files)
        return rows, _semantic_owner_hash(rows), False

    index_content = _file_at_revision(repo_root, revision_sha, REGISTRY_INDEX)
    shard_paths = {
        path
        for path in registry_artifacts
        if PurePosixPath(path).suffix.lower() == ".json" and path != REGISTRY_INDEX
    }
    shard_contents = {
        path.removeprefix(prefix): _file_at_revision(repo_root, revision_sha, path)
        for path in sorted(shard_paths)
    }
    rows, semantic_hash = _parse_owner_registry(
        index_content,
        shard_contents,
        tracked_files,
    )
    return rows, semantic_hash, True


def _changed_files(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return every changed path, including both rename/copy endpoints."""
    output = _git(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        "--find-copies=50%",
        "--find-copies-harder",
        base_sha,
        head_sha,
        text=False,
    )
    tokens = [token for token in output.split(b"\0") if token]
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        status = tokens[index].decode("ascii", errors="strict")
        index += 1
        path_count = 2 if status[0] in {"C", "R"} else 1
        if index + path_count > len(tokens):
            raise GateError("git diff emitted a truncated name-status record")
        for token in tokens[index : index + path_count]:
            try:
                paths.add(token.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise GateError("changed paths must be valid UTF-8") from exc
        index += path_count
    return sorted(paths)


def _tracked_files(repo_root: Path, head_sha: str) -> list[str]:
    """Return sorted repository-relative files present at the exact head."""
    output = _git(
        repo_root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        head_sha,
        text=False,
    )
    return sorted(path.decode("utf-8") for path in output.split(b"\0") if path)


def _validate_selector_matches(
    rows: list[OwnerRow],
    tracked_files: list[str],
    *,
    source: str = "canonical owner",
) -> None:
    """Reject stale selectors and files claimed by multiple owners."""
    for row in rows:
        for selector in row.selectors:
            if not any(fnmatch.fnmatchcase(path, selector) for path in tracked_files):
                if source == "canonical owner":
                    raise GateError(
                        f"canonical owner selector matches no exact-head file: {selector}"
                    )
                raise GateError(f"{source} selector matches no exact-revision file: {selector}")

    for path in tracked_files:
        matching_owner_ids = sorted(
            row.id
            for row in rows
            if any(fnmatch.fnmatchcase(path, selector) for selector in row.selectors)
        )
        if len(matching_owner_ids) > 1:
            raise GateError(
                f"{source} file matches selectors from multiple owners: "
                f"{path} ({', '.join(matching_owner_ids)})"
            )


def _combined_owner_rows(
    base_rows: list[OwnerRow],
    head_rows: list[OwnerRow],
) -> list[OwnerRow]:
    """Combine exact-base and exact-head selectors without losing removed owners."""
    base_by_id = {row.id: row for row in base_rows}
    head_ids = {row.id for row in head_rows}
    combined: list[OwnerRow] = []
    for head_row in head_rows:
        base_row = base_by_id.get(head_row.id)
        selectors = tuple(
            dict.fromkeys((*(() if base_row is None else base_row.selectors), *head_row.selectors))
        )
        combined.append(
            OwnerRow(
                id=head_row.id,
                decision=head_row.decision,
                owner=head_row.owner,
                selectors=selectors,
                guards=head_row.guards,
                source_paths=tuple(
                    dict.fromkeys(
                        (
                            *(() if base_row is None else base_row.source_paths),
                            *head_row.source_paths,
                        )
                    )
                ),
            )
        )
    combined.extend(row for row in base_rows if row.id not in head_ids)
    return combined


def _changed_owner_ids(
    base_rows: list[OwnerRow],
    head_rows: list[OwnerRow],
) -> set[str]:
    """Return stable owner IDs whose registry semantics differ across revisions."""
    base_by_id = {row.id: row for row in base_rows}
    head_by_id = {row.id: row for row in head_rows}

    def normalized(row: OwnerRow | None) -> tuple[Any, ...] | None:
        if row is None:
            return None
        return (
            row.decision,
            row.owner,
            tuple(sorted(row.selectors)),
            tuple(sorted(row.guards)),
        )

    return {
        owner_id
        for owner_id in base_by_id.keys() | head_by_id.keys()
        if normalized(base_by_id.get(owner_id)) != normalized(head_by_id.get(owner_id))
    }


def build_report(
    repo_root: Path,
    base: str,
    head: str,
    owner_table: str = DEFAULT_OWNER_TABLE,
) -> dict[str, Any]:
    """Build the deterministic owner-touch report for an exact revision pair."""
    base_sha = _resolve_revision(repo_root, base)
    head_sha = _resolve_revision(repo_root, head)
    base_files = _tracked_files(repo_root, base_sha)
    head_files = _tracked_files(repo_root, head_sha)
    base_rows, _, base_uses_registry = _ownership_at_revision(
        repo_root,
        base_sha,
        owner_table,
        base_files,
    )
    head_rows, head_semantic_hash, head_uses_registry = _ownership_at_revision(
        repo_root,
        head_sha,
        owner_table,
        head_files,
    )
    if base_uses_registry and not head_uses_registry:
        raise GateError("head revision removed the canonical owner registry")
    owner_rows = _combined_owner_rows(base_rows, head_rows)
    changed_owner_ids = _changed_owner_ids(base_rows, head_rows)
    changed_files = _changed_files(repo_root, base_sha, head_sha)
    changed_file_set = set(changed_files)

    touched_owners: list[dict[str, Any]] = []
    touched_indexes: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for row in owner_rows:
        matched_files = {
            path
            for path in changed_files
            if any(fnmatch.fnmatchcase(path, selector) for selector in row.selectors)
        }
        if row.id in changed_owner_ids:
            # Metadata-only edits are owner touches too. Attribute only the
            # exact artifact(s) that carried this row at base/head -- the
            # legacy Markdown table or the row's JSON shard -- rather than
            # every changed registry artifact.
            matched_files.update(changed_file_set.intersection(row.source_paths))
        if matched_files:
            display_key = (row.decision, row.owner, row.selectors)
            existing_index = touched_indexes.get(display_key)
            if existing_index is not None:
                existing_files = touched_owners[existing_index]["matched_files"]
                touched_owners[existing_index]["matched_files"] = sorted(
                    set(existing_files) | matched_files
                )
                continue
            touched_indexes[display_key] = len(touched_owners)
            touched_owners.append(
                {
                    "decision": row.decision,
                    "owner": row.owner,
                    "selectors": list(row.selectors),
                    "matched_files": sorted(matched_files),
                }
            )

    return {
        "version": REPORT_VERSION,
        "owner_table": owner_table,
        "owner_table_sha256": head_semantic_hash,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": changed_files,
        "touched_owners": touched_owners,
    }


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    """Return value as a mapping or fail closed with a field-specific error."""
    if not isinstance(value, dict):
        raise GateError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    """Return value as a list or fail closed with a field-specific error."""
    if not isinstance(value, list):
        raise GateError(f"{field} must be an array")
    return value


def verify_completion(
    completion: dict[str, Any],
    expected_report: dict[str, Any],
) -> dict[str, Any]:
    """Verify terminal functional evidence against a freshly derived report."""
    status = completion.get("status")
    terminal_statuses = {"ready-to-merge", "advisory-with-deferred"}
    if status in {"blocked", "superseded"}:
        return {
            "verified": True,
            "terminal_evidence_required": False,
            "status": status,
        }
    if status not in terminal_statuses:
        raise GateError(f"unsupported completion status: {status!r}")

    evidence = _require_mapping(completion.get("architecture_evidence"), "architecture_evidence")
    if evidence.get("version") != EVIDENCE_VERSION:
        raise GateError(f"architecture_evidence.version must be {EVIDENCE_VERSION!r}")

    embedded_report = _require_mapping(
        evidence.get("owner_touch_report"),
        "architecture_evidence.owner_touch_report",
    )
    if embedded_report != expected_report:
        raise GateError("owner_touch_report does not match fresh exact-head detection")

    touched_decisions = {item["decision"] for item in expected_report["touched_owners"]}
    classification = evidence.get("classification")
    if touched_decisions and classification not in OWNER_CLASSIFICATIONS:
        raise GateError(
            "classification self-exempts a deterministic owner touch; "
            "use owner-extension, new-owner, or split-authority-repair"
        )

    functional_tests = _require_list(
        evidence.get("functional_tests"),
        "architecture_evidence.functional_tests",
    )
    covered_decisions: set[str] = set()
    seen_test_ids: set[str] = set()
    expected_head = expected_report["head_sha"]
    for index, item in enumerate(functional_tests):
        test = _require_mapping(item, f"functional_tests[{index}]")
        test_id = test.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise GateError(f"functional_tests[{index}].test_id must be non-empty")
        if test_id in seen_test_ids:
            raise GateError(f"duplicate functional test id: {test_id}")
        seen_test_ids.add(test_id)
        if test.get("outcome") != "passed":
            raise GateError(f"functional test {test_id!r} did not pass")
        if test.get("head_sha") != expected_head:
            raise GateError(f"functional test {test_id!r} was not run at exact head")
        for field in ("command", "run_evidence"):
            value = test.get(field)
            if not isinstance(value, str) or not value.strip():
                raise GateError(f"functional test {test_id!r} has no {field}")

        owner_decisions = _require_list(
            test.get("owner_decisions"),
            f"functional_tests[{index}].owner_decisions",
        )
        for decision in owner_decisions:
            if not isinstance(decision, str) or not decision.strip():
                raise GateError(f"functional test {test_id!r} has an invalid owner decision")
            if decision not in touched_decisions:
                raise GateError(
                    f"functional test {test_id!r} cites untouched owner decision {decision!r}"
                )
            covered_decisions.add(decision)

    missing = sorted(touched_decisions - covered_decisions)
    if missing:
        raise GateError(
            "missing executed functional evidence for owner decisions: " + ", ".join(missing)
        )

    return {
        "verified": True,
        "terminal_evidence_required": True,
        "status": status,
        "owner_table_sha256": expected_report["owner_table_sha256"],
        "touched_owner_count": len(touched_decisions),
        "functional_test_ids": sorted(seen_test_ids),
    }


def _parser() -> argparse.ArgumentParser:
    """Build the non-interactive command-line interface."""
    parser = argparse.ArgumentParser(
        description=(
            "Detect canonical-owner touches from exact git revisions or verify "
            "a shepherd-driver completion return against fresh detection."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("detect", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo-root", type=Path, default=Path("."))
        child.add_argument("--base", required=True, help="Exact base revision or commit SHA.")
        child.add_argument("--head", required=True, help="Exact head revision or commit SHA.")
        child.add_argument(
            "--owner-table",
            default=DEFAULT_OWNER_TABLE,
            help="Repository-relative canonical owner table path.",
        )
        if command == "verify":
            child.add_argument(
                "--completion",
                type=Path,
                required=True,
                help="Path to the completion return JSON.",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run detection or verification with JSON stdout and diagnostics stderr."""
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            args.repo_root.resolve(),
            args.base,
            args.head,
            args.owner_table,
        )
        if args.command == "detect":
            result = report
        else:
            completion = json.loads(args.completion.read_text(encoding="utf-8"))
            result = verify_completion(_require_mapping(completion, "completion"), report)
    except (GateError, OSError, json.JSONDecodeError) as exc:
        print(f"[x] owner-touch gate failed: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
