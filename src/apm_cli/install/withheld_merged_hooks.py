"""Merged-config hook entries that match a withheld package's own hooks.

Targets such as Claude, Cursor and Codex merge hook entries into one shared
config file; ownership lives in an ``_apm_source`` marker or the hooks
sidecar. When the executables gate withholds a package's hooks, an entry in
that file that renders exactly as the package's own hook is named unless it
is the package's receipted deployment (its marker, while the lock still
records the package deploying hooks: install retires that and
``exec-status-current`` reports it). Install keeps a named entry and warns,
``apm audit --ci`` fails. Nothing is removed. Rendering reuses the
integrator's read-only steps.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ENTRY_REASON = "an entry matching its hook is not attributed to another source"


def withheld_merged_hook_entries(
    package_info: Any,
    project_root: Path,
    targets: Iterable[Any],
    *,
    user_scope: bool = False,
    receipted: bool = True,
) -> list[str]:
    """``<config path>: <event>`` for each matching merged entry no marker attributes.

    *receipted* says the lock still records the package as deploying hooks.
    Only then does a marker attributing an entry to the package excuse it
    (install retires it, ``exec-status-current`` reports it); without that
    record an attributed match is named like an unattributed one.

    Raises :class:`~apm_cli.install.withheld_hooks.HookPathsUnknown` when a
    config or the package's hooks cannot be read, so callers fail closed.
    """
    from apm_cli.install.withheld_hooks import (
        HookPathsUnknown,
        _require_parent_inside,
        present_hook_path,
    )
    from apm_cli.integration import hook_integrator as hooks
    from apm_cli.utils.path_security import has_symlink_component

    integrator = hooks.HookIntegrator()
    package_name = integrator._get_package_name(package_info, project_root)
    marker, legacy = integrator._get_hook_source_markers(package_info, project_root, package_name)
    owned = {marker, *legacy}
    found: set[str] = set()
    for target in targets or ():
        config = hooks._MERGE_HOOK_TARGETS.get(getattr(target, "name", ""))
        if config is None:
            continue
        json_path = project_root / target.root_dir / config.config_filename
        if not present_hook_path(json_path, json_path.as_posix()):
            continue
        if user_scope and Path(target.root_dir).is_absolute():
            # A supported user root outside $HOME (CLAUDE_CONFIG_DIR, CODEX_HOME).
            rel = json_path.as_posix()
        else:
            rel = json_path.relative_to(project_root).as_posix()
            _require_parent_inside(json_path, project_root, rel)
        if not user_scope and has_symlink_component(project_root, json_path):
            raise HookPathsUnknown(f"{rel} is reached through a symlink; inspect it manually")
        try:
            container = _owned_view(json_path, config, user_scope)
            wanted = _rendered_keys(
                integrator, package_info, package_name, target, config, project_root, user_scope
            )
        except (OSError, ValueError, TypeError) as exc:
            raise HookPathsUnknown(f"{rel}: {exc}") from exc
        for event, entries in container.items():
            for entry in entries:
                source = entry.get("_apm_source") if isinstance(entry, dict) else None
                if source is not None and not isinstance(source, str):
                    raise HookPathsUnknown(f"{rel}: event {event!a} has invalid ownership metadata")
                if isinstance(source, str) and not source.strip():
                    source = None  # a blank marker attributes nothing
                matches = isinstance(entry, dict) and integrator._hook_entry_content_key(
                    entry
                ) in wanted.get(event, ())
                if matches and source == package_name and source not in owned:
                    # A bare leaf marker another locked package could share: never skip it.
                    raise HookPathsUnknown(
                        f"attribution of {rel}: {event} is ambiguous; inspect it manually"
                    )
                if matches and (source is None or (source in owned and not receipted)):
                    found.add(f"{rel}: {event}")
    return sorted(found)


def _owned_view(json_path: Path, config: Any, user_scope: bool) -> dict:
    """The config's hook container with sidecar ownership markers re-applied."""
    from apm_cli.integration import hook_integrator as hooks
    from apm_cli.integration.hook_ownership import reinject_apm_source_from_sidecar

    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the config is not a JSON object")
    if config.event_container_key not in data:
        return {}
    container = data[config.event_container_key]
    if not isinstance(container, dict):
        raise ValueError(f"'{config.event_container_key}' is not an object")
    for event, entries in container.items():
        if not isinstance(entries, list):
            raise ValueError(f"event {event!a} is not a list")
    sidecar = json_path.parent / hooks._APM_HOOKS_SIDECAR
    if config.schema_strict and sidecar.is_file():
        try:
            owners = json.loads(sidecar.read_text(encoding="utf-8"))
        except ValueError:
            owners = None  # unreadable ownership proves nothing: entries stay unattributed
        if isinstance(owners, dict):
            reinject_apm_source_from_sidecar(container, owners)
    return container


def _rendered_keys(
    integrator: Any,
    package_info: Any,
    package_name: str,
    target: Any,
    config: Any,
    project_root: Path,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Content keys of the entries install would merge for the package, per event."""
    from apm_cli.integration import hook_integrator as hooks
    from apm_cli.integration import hook_native_formats as formats

    key = config.target_key
    install_path = Path(package_info.install_path)
    event_map = hooks._HOOK_EVENT_MAP.get(key, {})
    plain = {
        "claude": formats._to_claude_hook_entries,
        "codex": formats._to_codex_hook_entries,
        "gemini": formats._to_gemini_hook_entries,
    }
    from apm_cli.install.withheld_hooks import _require_readable_hook_dirs

    keys: dict[str, set[str]] = {}
    _require_readable_hook_dirs(install_path)
    sources = integrator.select_deployable_hook_sources(install_path, (key,))
    for hook_file in sources.descriptors_for(key):
        data = integrator._parse_hook_json(hook_file)
        if data is None:
            raise ValueError(f"cannot parse hook source {Path(hook_file).name}")
        rewritten, _scripts = integrator._rewrite_hooks_data(
            data,
            install_path,
            package_name,
            key,
            hook_file_dir=Path(hook_file).parent,
            root_dir=target.root_dir,
            deploy_root=integrator._deploy_root_for_hook_rewrite(project_root, user_scope),
            home_reference=integrator._home_reference_for_hook_rewrite(target, user_scope),
        )
        for raw_event, entries in (rewritten.get("hooks") or {}).items():
            if not isinstance(entries, list):
                continue
            event = event_map.get(raw_event, raw_event)
            if key == "cursor":
                native = formats._to_cursor_hook_entries(
                    entries, event, foreign=raw_event in event_map
                )
            elif key == "antigravity":
                native = formats._to_antigravity_hook_entries(entries, event)
            else:
                native = plain.get(key, list)(entries)
            for entry in (*native, *formats._legacy_hook_entries(entries, key)):
                if isinstance(entry, dict):
                    keys.setdefault(event, set()).add(integrator._hook_entry_content_key(entry))
    return keys
