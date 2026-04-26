"""Lockfile enrichment for pack-time metadata."""

from datetime import datetime, timezone
from typing import Dict, List, Tuple, Union

from ..deps.lockfile import LockFile


# Authoritative mapping of target names to deployed-file path prefixes.
_TARGET_PREFIXES = {
    "copilot": [".github/"],
    "vscode": [".github/"],
    "claude": [".claude/"],
    "cursor": [".cursor/"],
    "opencode": [".opencode/"],
    "codex": [".codex/", ".agents/"],
    "all": [".github/", ".claude/", ".cursor/", ".opencode/", ".codex/", ".agents/"],
}

# Cross-target path equivalences for skills/ and agents/ directories.
# Only these two directory types are semantically identical across targets;
# commands, instructions, hooks are target-specific and are NOT mapped.
#
# .github/ is the canonical interop prefix -- install always creates it, so
# all non-github targets map FROM .github/.  The copilot target additionally
# maps FROM .claude/ for the common case of Claude-first projects packing
# for Copilot.  Cursor/opencode sources are niche; if someone publishes
# skills exclusively under .cursor/, they must pack with --target cursor.
_CROSS_TARGET_MAPS: Dict[str, Dict[str, str]] = {
    "claude": {
        ".github/skills/": ".claude/skills/",
        ".github/agents/": ".claude/agents/",
    },
    "vscode": {
        ".claude/skills/": ".github/skills/",
        ".claude/agents/": ".github/agents/",
    },
    "copilot": {
        ".claude/skills/": ".github/skills/",
        ".claude/agents/": ".github/agents/",
    },
    "cursor": {
        ".github/skills/": ".cursor/skills/",
        ".github/agents/": ".cursor/agents/",
    },
    "opencode": {
        ".github/skills/": ".opencode/skills/",
        ".github/agents/": ".opencode/agents/",
    },
    "codex": {
        ".github/skills/": ".agents/skills/",
        ".github/agents/": ".codex/agents/",
    },
}


def _filter_files_by_target(
    deployed_files: List[str], target: Union[str, List[str]]
) -> Tuple[List[str], Dict[str, str]]:
    """Filter deployed file paths by target prefix, with cross-target mapping.

    When files are deployed under one target prefix (e.g. ``.github/skills/``)
    but the pack target is different (e.g. ``claude``), skills and agents are
    remapped to the equivalent target path.  Commands, instructions, and hooks
    are NOT remapped -- they are target-specific.

    *target* may be a single string or a list of strings.  For a list, the
    union of all relevant prefixes and cross-target maps is used.

    Returns:
        A tuple of ``(filtered_files, path_mappings)`` where *path_mappings*
        maps ``bundle_path -> disk_path`` for any file that was cross-target
        remapped.  Direct matches have no entry in the dict.
    """
    if isinstance(target, list):
        # Union all prefixes for the targets in the list
        prefixes: List[str] = []
        seen_prefixes: set = set()
        for t in target:
            for p in _TARGET_PREFIXES.get(t, []):
                if p not in seen_prefixes:
                    seen_prefixes.add(p)
                    prefixes.append(p)
        # Union all cross-target maps
        # NOTE: dict.update() means the last target's mapping wins when
        # multiple targets map the same source prefix. In practice this
        # is benign -- common multi-target combos (e.g. claude+copilot)
        # match prefixes directly without needing cross-maps.
        cross_map: Dict[str, str] = {}
        for t in target:
            cross_map.update(_CROSS_TARGET_MAPS.get(t, {}))
    else:
        prefixes = _TARGET_PREFIXES.get(target, _TARGET_PREFIXES["all"])
        cross_map = _CROSS_TARGET_MAPS.get(target, {})

    direct = [f for f in deployed_files if any(f.startswith(p) for p in prefixes)]

    path_mappings: Dict[str, str] = {}
    if cross_map:
        direct_set = set(direct)
        for f in deployed_files:
            if f in direct_set:
                continue
            for src_prefix, dst_prefix in cross_map.items():
                if f.startswith(src_prefix):
                    mapped = dst_prefix + f[len(src_prefix):]
                    if mapped not in direct_set:
                        direct.append(mapped)
                        direct_set.add(mapped)
                        path_mappings[mapped] = f
                    break

    return direct, path_mappings


def enrich_lockfile_for_pack(
    lockfile: LockFile,
    fmt: str,
    target: Union[str, List[str]],
) -> str:
    """Create an enriched copy of the lockfile YAML with a ``pack:`` section.

    Filters each dependency's ``deployed_files`` to only include paths
    matching the pack *target*, so the bundle lockfile is consistent with
    the files actually shipped in the bundle.

    Does NOT mutate the original *lockfile* object  -- serialises a copy and
    prepends the pack metadata.

    Args:
        lockfile: The resolved lockfile to enrich.
        fmt: Bundle format (``"apm"`` or ``"plugin"``).
        target: Effective target used for packing (e.g. ``"copilot"``, ``"claude"``,
            ``"all"``).  May also be a list of target strings for multi-target
            packing.  The internal alias ``"vscode"`` is also accepted.

    Returns:
        A YAML string with the ``pack:`` block followed by the original
        lockfile content.
    """
    import yaml

    # Build a filtered lockfile YAML: each dep's deployed_files is narrowed
    # to only the paths matching the pack target (with cross-target mapping).
    all_mappings: Dict[str, str] = {}
    data = yaml.safe_load(lockfile.to_yaml())
    if data and "dependencies" in data:
        for dep in data["dependencies"]:
            if "deployed_files" in dep:
                filtered, mappings = _filter_files_by_target(
                    dep["deployed_files"], target
                )
                dep["deployed_files"] = filtered
                all_mappings.update(mappings)

    # Issue #887: strip packaging-time local-content fields from the bundle
    # lockfile. ``local_deployed_files`` / ``local_deployed_file_hashes``
    # describe the packager's own repo content, which is intentionally NOT
    # shipped in the bundle (see packer.py source-local guard). Leaving them
    # in the bundle lockfile would cause ``LockFile.from_yaml()`` on the
    # consumer side to synthesize a self-entry whose ``deployed_files`` do
    # not exist under the bundle source dir, breaking unpacker verification.
    if isinstance(data, dict):
        data.pop("local_deployed_files", None)
        data.pop("local_deployed_file_hashes", None)

    # Build the pack: metadata section (after filtering so we know if mapping
    # occurred).
    # Serialize target as a comma-joined string for backward compatibility
    # with consumers that expect a plain string in pack.target.
    target_str = ",".join(target) if isinstance(target, list) else target
    pack_meta: Dict = {
        "format": fmt,
        "target": target_str,
        "packed_at": datetime.now(timezone.utc).isoformat(),
    }
    if all_mappings:
        # Record the source prefixes that were remapped so consumers know the
        # bundle paths differ from the original lockfile.  Use the canonical
        # prefix keys from _CROSS_TARGET_MAPS rather than reverse-engineering
        # them from file paths.
        if isinstance(target, list):
            cross_map: Dict[str, str] = {}
            for t in target:
                cross_map.update(_CROSS_TARGET_MAPS.get(t, {}))
        else:
            cross_map = _CROSS_TARGET_MAPS.get(target, {})
        used_src_prefixes = set()
        for original in all_mappings.values():
            for src_prefix in cross_map:
                if original.startswith(src_prefix):
                    used_src_prefixes.add(src_prefix)
                    break
        pack_meta["mapped_from"] = sorted(used_src_prefixes)

    from ..utils.yaml_io import yaml_to_str

    pack_section = yaml_to_str({"pack": pack_meta})

    lockfile_yaml = yaml_to_str(data)
    return pack_section + lockfile_yaml
