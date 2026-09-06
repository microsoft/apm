"""Import provenance sidecar: ``.apm/.import-sources.json``.

Records where each imported file came from and the hashes on both sides so a
re-run can tell "unchanged", "source updated", and "user edited the import"
apart without any marker inside the imported files themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from apm_cli.utils.atomic_io import write_text_lf
from apm_cli.utils.content_hash import compute_file_hash

SIDECAR_NAME = ".import-sources.json"
Decision = Literal[
    "write", "unchanged", "refresh", "collision", "locally-modified", "source-missing"
]


@dataclass
class ImportRecord:
    source: str
    scope: str
    converter: str
    source_sha256: str
    output_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "scope": self.scope,
            "converter": self.converter,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
        }


@dataclass
class ImportSources:
    """In-memory view of the sidecar."""

    path: Path
    entries: dict[str, ImportRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, apm_dir: Path) -> ImportSources:
        path = apm_dir / SIDECAR_NAME
        sources = cls(path=path)
        if not path.is_file():
            return sources
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return sources
        raw_entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw_entries, dict):
            return sources
        for dest, record in raw_entries.items():
            if not isinstance(record, dict):
                continue
            try:
                sources.entries[str(dest)] = ImportRecord(
                    source=str(record["source"]),
                    scope=str(record.get("scope", "project")),
                    converter=str(record.get("converter", "")),
                    source_sha256=str(record.get("source_sha256", "")),
                    output_sha256=str(record.get("output_sha256", "")),
                )
            except KeyError:
                continue
        return sources

    def save(self) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "entries": {dest: rec.to_dict() for dest, rec in sorted(self.entries.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def decide(self, dest_rel: str, dest_abs: Path, source_hash: str | None) -> Decision:
        """Apply the idempotency table for one destination."""
        record = self.entries.get(dest_rel)
        exists = dest_abs.exists()
        if record is None:
            return "collision" if exists else "write"
        if source_hash is None:
            return "source-missing"
        if not exists:
            return "write"
        current = compute_file_hash(dest_abs) if dest_abs.is_file() else ""
        if current != record.output_sha256:
            return "locally-modified"
        if source_hash == record.source_sha256:
            return "unchanged"
        return "refresh"

    def record(
        self,
        dest_rel: str,
        *,
        source: str,
        scope: str,
        converter: str,
        source_hash: str,
        dest_abs: Path,
    ) -> None:
        self.entries[dest_rel] = ImportRecord(
            source=source,
            scope=scope,
            converter=converter,
            source_sha256=source_hash,
            output_sha256=compute_file_hash(dest_abs) if dest_abs.is_file() else "",
        )


def hash_source(path: Path | None) -> str | None:
    """Hash a source file, or every file of a source directory, deterministically."""
    if path is None or not path.exists():
        return None
    if path.is_file():
        return compute_file_hash(path)
    import hashlib

    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(compute_file_hash(child).encode())
    return f"sha256:{digest.hexdigest()}"
