"""Hook merge-config helpers extracted from merge_config.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within

from ._opts import HookIntegrateOpts, HookRewriteOpts
from .class_ import HookIntegrationResult, _to_gemini_hook_entries

_HOOK_EVENT_MAP: dict[str, dict[str, str]] = {
    "claude": {
        # Copilot camelCase -> Claude PascalCase
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
    },
    "gemini": {
        # Copilot / Claude -> Gemini
        "PreToolUse": "BeforeTool",
        "preToolUse": "BeforeTool",
        "PostToolUse": "AfterTool",
        "postToolUse": "AfterTool",
        "Stop": "SessionEnd",
    },
}


def _clear_prior_package_hooks(
    json_config: dict,
    event_name: str,
    package_name: str,
    reverse_map: dict[str, set[str]],
    cleared_events: set,
) -> None:
    """Drop prior entries owned by *package_name* for *event_name* (idempotent upsert).

    Only strips once per event per install run -- a package with multiple hook
    files targeting the same event contributes entries in turn, so stripping on
    every iteration would erase earlier files' fresh entries.  If *event_name*
    is already in *cleared_events*, returns immediately.

    Also clears alias events that normalise to *event_name* to handle
    corrupted installs with mixed-case event keys.
    """
    if event_name in cleared_events:
        return
    # Clear from the normalised event
    json_config["hooks"][event_name] = [
        e
        for e in json_config["hooks"][event_name]
        if not (isinstance(e, dict) and e.get("_apm_source") == package_name)
    ]
    # Also clear from any alias events that map to this normalised name.
    for alias in reverse_map.get(event_name, set()):
        if alias != event_name and alias in json_config["hooks"]:
            json_config["hooks"][alias] = [
                e
                for e in json_config["hooks"][alias]
                if not (isinstance(e, dict) and e.get("_apm_source") == package_name)
            ]
            # Remove the alias key entirely if now empty
            if not json_config["hooks"][alias]:
                del json_config["hooks"][alias]
    cleared_events.add(event_name)


def _dedup_hook_entries(entries: list) -> list:
    """Deduplicate hook entries by ``(_apm_source, content)`` key.

    Safety net for edge cases where multiple source files produce
    semantically identical entries for the same event.
    """
    seen_content: list[dict] = []
    deduped: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            deduped.append(entry)
            continue
        # Build comparison key (all fields except _apm_source)
        cmp = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
        source = entry.get("_apm_source")
        is_dup = False
        for seen in seen_content:
            if seen.get("_source") == source and seen.get("_cmp") == cmp:
                is_dup = True
                break
        if not is_dup:
            seen_content.append({"_source": source, "_cmp": cmp})
            deduped.append(entry)
    return deduped


def _empty_hook_result() -> HookIntegrationResult:
    """Return an empty hook integration result."""
    return HookIntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
    )


def _load_hook_json_config(json_path: Path) -> dict:
    """Load the merge-target JSON config, defaulting to an empty hooks map."""
    json_config: dict = {}
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as handle:
                json_config = json.load(handle)
        except (json.JSONDecodeError, OSError):
            json_config = {}
    json_config.setdefault("hooks", {})
    return json_config


def _build_reverse_event_map(event_map: dict[str, str]) -> dict[str, set[str]]:
    """Return a reverse mapping of normalised event names to aliases."""
    reverse_map: dict[str, set[str]] = {}
    for source_name, norm_name in event_map.items():
        reverse_map.setdefault(norm_name, set()).add(source_name)
    return reverse_map


def _mark_hook_entry_sources(entries: list, package_name: str) -> None:
    """Tag merged hook entries with their owning package."""
    for entry in entries:
        if isinstance(entry, dict):
            entry["_apm_source"] = package_name


def _merge_hook_events(
    json_config: dict,
    hooks: dict,
    target_key: str,
    package_name: str,
    cleared_events: set,
) -> None:
    """Merge rewritten hook events into the target JSON config."""
    event_map = _HOOK_EVENT_MAP.get(target_key, {})
    reverse_map = _build_reverse_event_map(event_map)
    target_hooks = json_config["hooks"]

    for raw_event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        event_name = event_map.get(raw_event_name, raw_event_name)
        target_hooks.setdefault(event_name, [])
        event_entries = _to_gemini_hook_entries(entries) if target_key == "gemini" else entries
        _mark_hook_entry_sources(event_entries, package_name)
        _clear_prior_package_hooks(
            json_config,
            event_name,
            package_name,
            reverse_map,
            cleared_events,
        )
        target_hooks[event_name].extend(event_entries)
        target_hooks[event_name] = _dedup_hook_entries(target_hooks[event_name])


def _rewrite_hook_file(
    self,
    package_info,
    hook_file: Path,
    root_dir: str,
    target_key: str,
    deploy_root: Path | None = None,
):
    """Parse and rewrite one hook file for a merge-based target."""
    data = self._parse_hook_json(hook_file)
    if data is None:
        return None
    package_name = self._get_package_name(package_info)
    return self._rewrite_hooks_data(
        data,
        HookRewriteOpts(
            package_path=package_info.install_path,
            package_name=package_name,
            target=target_key,
            hook_file_dir=hook_file.parent,
            root_dir=root_dir,
            deploy_root=deploy_root,
        ),
    )


def _copy_hook_scripts(
    self,
    scripts: list,
    project_root: Path,
    opts: HookIntegrateOpts,
) -> tuple[list[Path], int, int]:
    """Copy or adopt rewritten hook scripts for one source hook file."""
    target_paths: list[Path] = []
    scripts_copied = 0
    scripts_adopted = 0

    for source_file, target_rel in scripts:
        target_script = project_root / target_rel
        ensure_path_within(target_script, project_root)
        if self.is_content_identical_to_source(target_script, source_file):
            target_paths.append(target_script)
            scripts_adopted += 1
            continue
        if self.check_collision(
            target_script,
            target_rel,
            opts.managed_files,
            opts.force,
            diagnostics=opts.diagnostics,
        ):
            continue
        target_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_script)
        scripts_copied += 1
        target_paths.append(target_script)

    return target_paths, scripts_copied, scripts_adopted
