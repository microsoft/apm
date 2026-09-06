"""Create or merge ``apm.yml`` for discovery ``--apply``.

Uses ruamel round-trip loading so comments and ordering in an existing
manifest survive. Never removes anything the user declared.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.core.target_catalog import manifest_target_names
from apm_cli.utils.yaml_io import dump_yaml_roundtrip, load_yaml_roundtrip


def _entry_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, Mapping):
        name = entry.get("name")
        return str(name) if name else None
    return None


def merge_mcp_entries(
    existing: list[Any], new: Iterable[Mapping[str, Any]]
) -> tuple[list[Any], list[str]]:
    """Append MCP entries whose name is not already declared.

    Returns the merged list and human-readable change notes. Existing entries
    always win: a string (registry) entry or a different self-defined block
    with the same name is left alone and the incoming entry is skipped.
    """
    merged = list(existing)
    notes: list[str] = []
    declared = {_entry_name(e) for e in existing} - {None}
    for entry in new:
        name = _entry_name(entry)
        if not name:
            continue
        if name in declared:
            notes.append(f"dependencies.mcp: {name} already declared; kept existing")
            continue
        merged.append(dict(entry))
        declared.add(name)
        notes.append(f"dependencies.mcp: added {name}")
    return merged, notes


def apply_manifest_delta(
    manifest_path: Path,
    *,
    targets: Iterable[str],
    mcp_entries: Iterable[Mapping[str, Any]],
    create_config: Mapping[str, Any] | None,
) -> list[str]:
    """Merge *targets* and *mcp_entries* into the manifest, creating it if needed."""
    notes: list[str] = []
    if not manifest_path.is_file():
        if create_config is None:
            return notes
        from apm_cli.commands._helpers import _create_minimal_apm_yml

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _create_minimal_apm_yml(dict(create_config), target_path=manifest_path)
        notes.append(f"created {manifest_path.name}")

    data = load_yaml_roundtrip(manifest_path)
    if not isinstance(data, Mapping):
        raise ValueError(f"{manifest_path} is not a mapping")

    wanted = [t for t in targets if t in manifest_target_names()]
    if wanted:
        try:
            current = parse_targets_field(dict(data))
        except Exception:  # invalid or empty targets block: rebuild it
            current = []
        merged_targets = list(dict.fromkeys([*current, *wanted]))
        if merged_targets != current or "targets" not in data:
            if "target" in data:
                del data["target"]
                notes.append("targets: converted legacy singular 'target' key")
            data["targets"] = merged_targets
            added = [t for t in wanted if t not in current]
            if added:
                notes.append(f"targets: added {', '.join(added)}")

    mcp_entries = list(mcp_entries)
    if mcp_entries:
        dependencies = data.get("dependencies")
        if not isinstance(dependencies, Mapping):
            data["dependencies"] = {"apm": [], "mcp": []}
            dependencies = data["dependencies"]
        existing = dependencies.get("mcp")
        if not isinstance(existing, list):
            existing = []
        merged, mcp_notes = merge_mcp_entries(existing, mcp_entries)
        dependencies["mcp"] = merged
        notes.extend(mcp_notes)

    if notes:
        dump_yaml_roundtrip(data, manifest_path)
    return notes
