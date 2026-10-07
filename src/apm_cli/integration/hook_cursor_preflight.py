"""Read-only Cursor native validation and Claude-import coexistence checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.hook_contract import HookContractError, hook_handlers
from apm_cli.integration.hook_native_formats import (
    CURSOR_NATIVE_EVENTS,
    _to_cursor_hook_entries,
    validate_cursor_config,
)
from apm_cli.integration.hook_ownership import reinject_apm_source_from_sidecar
from apm_cli.integration.hook_source_selection import HookSourceSelection
from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component

if TYPE_CHECKING:
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.models.apm_package import PackageInfo


def _read_config(path: Path, root: Path) -> dict[str, Any]:
    """Read existing config without following links or replacing malformed content."""
    ensure_path_within(path, root)
    if has_symlink_component(root, path):
        raise HookContractError("hook configuration must not be a symlink")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HookContractError(
            f"cannot read hook configuration {printable_ascii_text(path.name)}"
        ) from exc
    if not isinstance(data, dict):
        raise HookContractError("hook configuration must be a JSON object")
    return data


def _existing_hooks(path: Path, root: Path) -> dict[str, Any]:
    data = _read_config(path, root)
    sidecar = _read_config(path.with_name("apm-hooks.json"), root)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        raise HookContractError("existing hook configuration requires event arrays")
    if sidecar:
        reinject_apm_source_from_sidecar(hooks, sidecar)
    return data


def _without_retiring_owner(document: dict[str, Any], owners: set[str]) -> dict[str, Any]:
    """Project the existing ownership decision without touching user entries."""
    return {
        "hooks": {
            event: [
                entry
                for entry in entries
                if not isinstance(entry, dict)
                or not isinstance(entry.get("_apm_source"), str)
                or entry["_apm_source"] not in owners
            ]
            for event, entries in document.get("hooks", {}).items()
        }
    }


def _action_keys(
    document: dict[str, Any],
    event_map: dict[str, str],
    roots: tuple[str, ...],
    owners: set[str],
    *,
    owned: bool = False,
) -> set[tuple[str, str, str]]:
    """Compare declared actions, not matcher semantics or executable contents."""
    result: set[tuple[str, str, str]] = set()
    for binding in hook_handlers(document):
        event = event_map.get(binding.event)
        if event is None:
            continue
        value = binding.value
        source = value.get("_apm_source")
        if owned or (isinstance(source, str) and source in owners):
            result.add((event, "owner", "current-package"))
        kind = value.get("type", "command")
        content = value.get("prompt" if kind == "prompt" else "command")
        if not isinstance(content, str):
            continue
        for root in roots:
            content = content.replace(root + "/", "$APM_PACKAGE/")
        result.add((event, str(kind), content))
    return result


def preflight_cursor_hooks(
    integrator: HookIntegrator,
    package_info: PackageInfo,
    project_root: Path,
    selection: HookSourceSelection,
    event_maps: dict[str, dict[str, str]],
    *,
    user_scope: bool = False,
    retiring_targets: frozenset[str] = frozenset(),
) -> None:
    """Reject unsupported Cursor hooks or potential double activation before writes.

    Import settings are intentionally not read or changed: they are user-specific
    and can change after installation. A shared action must have one deployment
    route, not two configurations that are safe only while imports are disabled.
    """
    from apm_cli.integration.targets import KNOWN_TARGETS

    cursor_root = project_root / KNOWN_TARGETS["cursor"].root_dir
    cursor_files = selection.descriptors_for("cursor")
    claude_files = selection.descriptors_for("claude")
    if not cursor_files and not claude_files:
        return
    home = Path.home()
    native_paths = [(cursor_root / "hooks.json", project_root)]
    if home != project_root:
        native_paths.append((home / KNOWN_TARGETS["cursor"].root_dir / "hooks.json", home))
    if not cursor_files and not any(path.exists() for path, _ in native_paths):
        return
    package_name = integrator._get_package_name(package_info, project_root)
    source, legacy = integrator._get_hook_source_markers(package_info, project_root, package_name)
    owners = {source, *legacy}
    roots = tuple(
        sorted(
            {
                f"{base}/{name}/hooks/{package_name}"
                for base in (project_root.as_posix(), home.as_posix())
                for name in (".claude", ".cursor")
            }
            | {f"{name}/hooks/{package_name}" for name in (".claude", ".cursor")},
            key=len,
            reverse=True,
        )
    )
    native_map = {event: event for event in CURSOR_NATIVE_EVENTS}
    cursor_map = event_maps["cursor"]
    claude_map = {
        **cursor_map,
        **{
            alias: cursor_map[native]
            for alias, native in event_maps["claude"].items()
            if native in cursor_map
        },
    }

    def source_actions(files: list[Path], *, cursor: bool) -> set[tuple[str, str, str]]:
        actions: set[tuple[str, str, str]] = set()
        for path in files:
            document = integrator._parse_hook_json(path)
            if document is None or not isinstance(document.get("hooks"), dict):
                raise HookContractError(f"invalid hook source {printable_ascii_text(path.name)}")
            rewritten, _ = integrator._rewrite_hooks_data(
                document,
                package_info.install_path,
                package_name,
                "cursor",
                hook_file_dir=path.parent,
                deploy_root=integrator._deploy_root_for_hook_rewrite(project_root, user_scope),
            )
            if cursor:
                if document.keys() - {"version", "hooks", "description"}:
                    raise HookContractError(
                        "unsupported Cursor source fields "
                        f"{sorted(document.keys() - {'version', 'hooks', 'description'})!r}; "
                        "no settings were discarded"
                    )
                if "description" in document and not isinstance(document["description"], str):
                    raise HookContractError("Cursor source description must be a string")
                if "version" in document and (
                    type(document["version"]) is not int or document["version"] != 1
                ):
                    raise HookContractError("Cursor source requires version 1 when specified")
                for event, entries in rewritten["hooks"].items():
                    _to_cursor_hook_entries(
                        entries, cursor_map.get(event, event), foreign=event in cursor_map
                    )
            actions.update(
                _action_keys(
                    rewritten,
                    {**native_map, **cursor_map} if cursor else claude_map,
                    roots,
                    owners,
                    owned=True,
                )
            )
        return actions

    try:
        cursor_actions = source_actions(cursor_files, cursor=True)
        claude_actions = source_actions(claude_files, cursor=False)
        if cursor_files:
            existing = _existing_hooks(cursor_root / "hooks.json", project_root)
            candidate = copy.deepcopy(existing)
            candidate.setdefault("version", 1)
            candidate.update(_without_retiring_owner(candidate, owners))
            candidate["hooks"] = {
                event: entries for event, entries in candidate["hooks"].items() if entries
            }
            validate_cursor_config(candidate)
            imports = [
                (project_root / ".claude/settings.local.json", project_root),
                (project_root / ".claude/settings.json", project_root),
                (home / ".claude/settings.json", home),
            ]
            for path, root in dict.fromkeys(imports):
                imported = _existing_hooks(path, root)
                if "claude" in retiring_targets and path == project_root / ".claude/settings.json":
                    imported = _without_retiring_owner(imported, owners)
                claude_actions.update(
                    _action_keys(
                        {"hooks": imported.get("hooks", {})},
                        cursor_map,
                        roots,
                        owners,
                    )
                )
        if claude_files:
            for path, root in native_paths:
                existing = _existing_hooks(path, root)
                if "cursor" in retiring_targets and path == cursor_root / "hooks.json":
                    existing = _without_retiring_owner(existing, owners)
                cursor_actions.update(
                    _action_keys(
                        {"hooks": existing.get("hooks", {})},
                        native_map,
                        roots,
                        owners,
                    )
                )
        if cursor_actions & claude_actions:
            raise HookContractError(
                "Cursor native hooks overlap Claude import and may run twice. "
                "Select one hook target per dependency; existing Claude hooks can use "
                "Cursor's third-party import instead. No import setting was changed"
            )
    except HookContractError as exc:
        raise HookContractError(
            f"Cannot install hooks for {printable_ascii_text(package_name)}: "
            f"{printable_ascii_text(str(exc))}. "
            "Use supported Cursor-native hooks or a single Claude-import route, then reinstall."
        ) from exc
