"""The one reader of the APMLifecycle escaped-defect ledger, in either layout.

Directory layout (``tests/fixtures/lifecycle_bug_ledger/``): one JSON file per
row, named by the row's stable id, so concurrent pull requests that add rows
never edit the same file:

- ``ledger.json``: ``schema_version`` and ``scope``;
- ``property_catalog/<id>.json``, ``bugs/<issue>.json``,
  ``known_gaps/<id>.json`` and ``lifecycle_contracts/<id>.json``.

Legacy layout (``tests/fixtures/lifecycle_bug_ledger.json``): one document.
It is read only when the directory is absent, so a checkout or revision from
before the split still loads. Both layouts yield the same document: rows are
ordered by their id (``bugs`` numerically by issue).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LEDGER_DIR = "tests/fixtures/lifecycle_bug_ledger"
LEGACY_LEDGER = "tests/fixtures/lifecycle_bug_ledger.json"
HEADER_FILE = "ledger.json"
# Row table -> the field that names the row and its file.
ROW_TABLES: Mapping[str, str] = {
    "property_catalog": "id",
    "bugs": "issue",
    "lifecycle_contracts": "id",
    "known_gaps": "id",
}


class LedgerError(ValueError):
    """The ledger files do not form one valid ledger document."""


def _row_key(table: str, row: Mapping[str, Any]) -> tuple[int, str]:
    value = row[ROW_TABLES[table]]
    return (value, "") if table == "bugs" else (0, value)


def ledger_from_files(files: Mapping[str, str]) -> dict[str, Any]:
    """Assemble the ledger document from ``{path under LEDGER_DIR: JSON text}``."""
    if HEADER_FILE not in files:
        raise LedgerError(f"{LEDGER_DIR}/{HEADER_FILE} is missing")
    ledger: dict[str, Any] = json.loads(files[HEADER_FILE])
    if not isinstance(ledger, dict) or set(ledger) & set(ROW_TABLES):
        raise LedgerError(f"{HEADER_FILE} must be an object without row tables")
    tables: dict[str, list[dict[str, Any]]] = {table: [] for table in ROW_TABLES}
    for path, text in files.items():
        if path == HEADER_FILE:
            continue
        table, _, name = path.partition("/")
        if table not in tables or "/" in name or not name.endswith(".json"):
            raise LedgerError(f"Unexpected ledger file: {LEDGER_DIR}/{path}")
        row = json.loads(text)
        field = ROW_TABLES[table]
        if not isinstance(row, dict) or str(row.get(field)) != name[: -len(".json")]:
            raise LedgerError(f"{LEDGER_DIR}/{path}: file name must equal the row {field}")
        tables[table].append(row)
    for table, rows in tables.items():
        ledger[table] = sorted(rows, key=lambda row, table=table: _row_key(table, row))
    return ledger


def ledger_files(ledger: Mapping[str, Any]) -> dict[str, str]:
    """Split a ledger document into ``{path under LEDGER_DIR: JSON text}``."""
    header = {key: value for key, value in ledger.items() if key not in ROW_TABLES}
    files = {HEADER_FILE: _dump(header)}
    for table, field in ROW_TABLES.items():
        for row in ledger.get(table, []):
            path = f"{table}/{row[field]}.json"
            if path in files:
                raise LedgerError(f"Duplicate ledger row: {path}")
            files[path] = _dump(row)
    return files


def write_ledger(root: Path, ledger: Mapping[str, Any]) -> None:
    """Write ``ledger`` in the directory layout under ``root``, replacing any prior copy."""
    directory = root / LEDGER_DIR
    if directory.exists():
        shutil.rmtree(directory)
    for path, text in ledger_files(ledger).items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="ascii", newline="\n")


def read_ledger(root: Path) -> dict[str, Any]:
    """Read the working-tree ledger: the directory if present, else the legacy file."""
    directory = root / LEDGER_DIR
    if directory.is_dir():
        return ledger_from_files(
            {
                path.relative_to(directory).as_posix(): path.read_text(encoding="ascii")
                for path in directory.rglob("*")
                if path.is_file()
            }
        )
    return json.loads((root / LEGACY_LEDGER).read_text(encoding="ascii"))


def read_ledger_at(root: Path, revision: str) -> dict[str, Any]:
    """Read the ledger committed at ``revision``, in whichever layout it has."""
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", revision, "--", LEDGER_DIR)
    paths = [path for path in listing.decode("utf-8").split("\0") if path]
    if not paths:
        return json.loads(_git(root, "show", f"{revision}:{LEGACY_LEDGER}").decode("ascii"))
    blobs = _read_blobs(root, revision, paths)
    prefix = f"{LEDGER_DIR}/"
    return ledger_from_files({path[len(prefix) :]: text for path, text in blobs.items()})


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise LedgerError("git is required to read a committed ledger")
    return subprocess.run(  # noqa: S603 - resolved executable, argument vector, no shell
        [executable, "--no-replace-objects", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def _read_blobs(root: Path, revision: str, paths: list[str]) -> dict[str, str]:
    """Read many blobs at ``revision`` with one ``git cat-file --batch`` call."""
    request = "".join(f"{revision}:{path}\n" for path in paths).encode("utf-8")
    output = _git(root, "cat-file", "--batch", stdin=request)
    blobs: dict[str, str] = {}
    offset = 0
    for path in paths:
        header_end = output.index(b"\n", offset)
        header = output[offset:header_end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise LedgerError(f"Cannot read {path} at {revision}")
        start = header_end + 1
        size = int(header[2])
        blobs[path] = output[start : start + size].decode("ascii")
        offset = start + size + 1
    return blobs
