"""Lockfile enrichment for pack-time metadata."""

import posixpath
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Union  # noqa: F401, UP035

from ..deps.lockfile import LockFile
from ..integration.targets import KNOWN_TARGETS

# Cross-target path equivalences for skills/ and agents/ directories.
# Only these two directory types are semantically identical across targets;
# commands, instructions, hooks are target-specific and are NOT mapped.
#
# .github/ is the canonical interop prefix -- install always creates it, so
# all non-github targets map FROM .github/.  The copilot target additionally
# maps FROM .claude/ for the common case of Claude-first projects packing
# for Copilot.  Cursor/opencode sources are niche; if someone publishes
# skills exclusively under .cursor/, they must pack with --target cursor.
#
# Windsurf converts agents -> skills (lossy: AGENTS.md format is collapsed
# into the windsurf skill envelope), so .github/agents/ maps to
# .windsurf/skills/.
_CROSS_TARGET_MAPS: dict[str, dict[str, str]] = {
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
    "windsurf": {
        ".github/skills/": ".windsurf/skills/",
        ".github/agents/": ".windsurf/skills/",
    },
    "agent-skills": {
        ".github/skills/": ".agents/skills/",
    },
}


def _all_target_prefixes() -> list[str]:
    """Union of pack prefixes for every real (deployable) target.

    A target is considered deployable when ``detect_by_dir`` or
    ``auto_create`` is True; ``copilot-cowork`` (both False) is excluded
    because it is an opt-in pseudo-target.

    Order is stable: KNOWN_TARGETS insertion order, with deduplication
    preserving first occurrence.  This keeps downstream YAML deterministic.
    """
    prefixes: list[str] = []
    seen: set[str] = set()
    for profile in KNOWN_TARGETS.values():
        if not (profile.detect_by_dir or profile.auto_create):
            continue
        for prefix in profile.effective_pack_prefixes:
            if prefix not in seen:
                seen.add(prefix)
                prefixes.append(prefix)
    return prefixes


def _get_target_prefixes(target: str) -> list[str]:
    """Resolve pack-prefixes for a single target name.

    Reads from ``KNOWN_TARGETS[target].effective_pack_prefixes``.  Special
    cases:

    * ``"all"`` -- union of every deployable target's prefixes (see
      :func:`_all_target_prefixes`).
    * ``"vscode"`` -- treated as an alias for ``"copilot"`` (both deploy
      to ``.github/``); kept for backward compatibility because
      ``vscode`` is a valid MCP-only adapter target_name.
    * Unknown targets -- fall back to the union, matching the previous
      behavior of falling through to the all-targets default.
    """
    if target == "all":
        return _all_target_prefixes()
    if target == "vscode":
        return list(KNOWN_TARGETS["copilot"].effective_pack_prefixes)
    profile = KNOWN_TARGETS.get(target)
    if profile is None:
        return _all_target_prefixes()
    return list(profile.effective_pack_prefixes)


def _filter_files_by_target(
    deployed_files: list[str], target: str | list[str]
) -> tuple[list[str], dict[str, str]]:
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
        prefixes: list[str] = []
        seen_prefixes: set = set()
        for t in target:
            for p in _get_target_prefixes(t):
                if p not in seen_prefixes:
                    seen_prefixes.add(p)
                    prefixes.append(p)
        # Union all cross-target maps
        # NOTE: dict.update() means the last target's mapping wins when
        # multiple targets map the same source prefix. In practice this
        # is benign -- common multi-target combos (e.g. claude+copilot)
        # match prefixes directly without needing cross-maps.
        cross_map: dict[str, str] = {}
        for t in target:
            cross_map.update(_CROSS_TARGET_MAPS.get(t, {}))
    else:
        prefixes = _get_target_prefixes(target)
        cross_map = _CROSS_TARGET_MAPS.get(target, {})

    direct = [f for f in deployed_files if any(f.startswith(p) for p in prefixes)]

    path_mappings: dict[str, str] = {}
    if cross_map:
        direct_set = set(direct)
        for f in deployed_files:
            if f in direct_set:
                continue
            for src_prefix, dst_prefix in cross_map.items():
                if f.startswith(src_prefix):
                    mapped = dst_prefix + f[len(src_prefix) :]
                    # Containment guard: normalise the remapped path and
                    # reject any result that escapes the destination prefix
                    # via traversal segments (e.g. "../../etc/passwd").
                    normalised = posixpath.normpath(mapped)
                    if ".." in normalised.split("/"):
                        continue
                    if not normalised.startswith(dst_prefix.rstrip("/")):
                        continue
                    # Preserve trailing slash (directory marker in lockfiles)
                    if mapped.endswith("/") and not normalised.endswith("/"):
                        normalised += "/"
                    mapped = normalised
                    if mapped not in direct_set:
                        direct.append(mapped)
                        direct_set.add(mapped)
                        path_mappings[mapped] = f
                    break

    return direct, path_mappings


def enrich_lockfile_for_pack(
    lockfile: LockFile,
    fmt: str,
    target: str | list[str],
    *,
    bundle_files: dict[str, str] | None = None,
) -> str:
    """Create an enriched copy of the lockfile YAML with a ``pack:`` section.

    Filters each dependency's ``deployed_files`` to only include paths
    matching the pack *target*, so the bundle lockfile is consistent with
    the files actually shipped in the bundle.

    Does NOT mutate the original *lockfile* object  -- serialises a copy and
    prepends the pack metadata.

    Args:
        lockfile: The resolved lockfile to enrich.
        fmt: Bundle format (``"plugin"`` or ``"apm"``).
        target: Effective target used for packing (e.g. ``"copilot"``, ``"claude"``,
            ``"all"``).  May also be a list of target strings for multi-target
            packing.  The internal alias ``"vscode"`` is also accepted.
        bundle_files: Optional mapping of bundle-relative path -> sha256 hex
            digest, embedded under ``pack.bundle_files``.  Used for plugin
            bundles whose flat layout differs from the project-relative
            ``deployed_files`` paths and so requires a separate manifest
            for integrity verification at install time (see issue #1098).

    Returns:
        A YAML string with the ``pack:`` block followed by the original
        lockfile content.
    """
    import yaml

    # Build a filtered lockfile YAML: each dep's deployed_files is narrowed
    # to only the paths matching the pack target (with cross-target mapping).
    all_mappings: dict[str, str] = {}
    data = yaml.safe_load(lockfile.to_yaml())
    if data and "dependencies" in data:
        for dep in data["dependencies"]:
            if "deployed_files" in dep:
                filtered, mappings = _filter_files_by_target(dep["deployed_files"], target)
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
    pack_meta: dict = {
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
            cross_map: dict[str, str] = {}
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

    if bundle_files:
        # Bundle-relative path -> sha256 hex digest. Used by
        # ``verify_bundle_integrity()`` at install time. Sorted for
        # deterministic YAML output.
        pack_meta["bundle_files"] = dict(sorted(bundle_files.items()))

    from ..utils.yaml_io import yaml_to_str

    pack_section = yaml_to_str({"pack": pack_meta})

    lockfile_yaml = yaml_to_str(data)
    return pack_section + lockfile_yaml
