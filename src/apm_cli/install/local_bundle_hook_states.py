"""Read merged hook configs and ownership sidecars for local-bundle hook trust (#5078).

Readers used by :mod:`apm_cli.install.local_bundle_hook_trust`: each merged
hook target's config and sidecar is read once, and bundle entries edited or
removed outside APM are found by content match against the sidecar record.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _MergedHookState:
    """One merged hook target's config and ownership sidecar, read once."""

    target: Any
    label: str
    config: dict[str, Any] | None
    sidecar: dict[str, Any] | None
    container: str
    config_unreadable: bool = False
    sidecar_unreadable: bool = False


def _merged_hook_states(project_root: Path, targets: Any) -> Iterator[_MergedHookState]:
    """Read every merged hook target's config and sidecar, each once (#5078)."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.hook_ownership import read_hook_object

    for target in targets or ():
        config = _MERGE_HOOK_TARGETS.get(getattr(target, "name", None))
        if config is None:
            continue
        json_path = Path(project_root) / target.root_dir / config.config_filename
        documents: list[dict[str, Any] | None] = []
        for path in (json_path, json_path.parent / _APM_HOOKS_SIDECAR):
            try:
                documents.append(read_hook_object(path))
            except (OSError, ValueError, TypeError):
                documents.append(None)
        yield _MergedHookState(
            target,
            Path(target.root_dir, config.config_filename).as_posix(),
            documents[0],
            documents[1],
            config.event_container_key,
            config_unreadable=documents[0] is None,
            sidecar_unreadable=documents[1] is None,
        )


def _merged_hook_documents(project_root: Path, targets: Any):
    """Yield ``(config, sidecar, container)`` for every merged hook target."""
    for state in _merged_hook_states(project_root, targets):
        if state.config_unreadable or state.sidecar_unreadable:
            # Unreadable ownership state cannot prove an approval.
            yield None, None, state.container
        else:
            yield state.config, state.sidecar, state.container


def _markers(entries_by_event: Any) -> list[str]:
    return [
        entry["_apm_source"]
        for entries in (entries_by_event.values() if isinstance(entries_by_event, dict) else ())
        for entry in (entries if isinstance(entries, list) else ())
        if isinstance(entry, dict) and isinstance(entry.get("_apm_source"), str)
    ]


def _modified_slugs(state: _MergedHookState) -> set[str]:
    """Bundles whose recorded entries no disk entry matches any more (edited or removed)."""
    import copy
    from collections import Counter

    from apm_cli.integration.hook_ownership import (
        parse_local_bundle_hook_source,
        reinject_apm_source_from_sidecar,
    )

    hooks = copy.deepcopy((state.config or {}).get(state.container))
    hooks = hooks if isinstance(hooks, dict) else {}
    # Only a content match proves ownership: an inline marker copied into an
    # edited entry must not pass for the bundle's recorded entry.
    for entries in hooks.values():
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict):
                entry.pop("_apm_source", None)
    reinject_apm_source_from_sidecar(hooks, state.sidecar or {})
    unmatched = Counter(_markers(state.sidecar)) - Counter(_markers(hooks))
    return {
        parsed[0]
        for marker in unmatched
        if (parsed := parse_local_bundle_hook_source(marker)) is not None
    }


def _recorded_slugs(state: _MergedHookState) -> set[str]:
    from apm_cli.integration.hook_ownership import parse_local_bundle_hook_source

    return {
        parsed[0]
        for marker in _markers(state.sidecar)
        if (parsed := parse_local_bundle_hook_source(marker)) is not None
    }


def _hook_commands(document: Any) -> list[str]:
    """Commands a per-file hook document runs, read from handler positions only (#5136).

    The shared hook grammar locates the handlers, so metadata that merely names
    a script (``env.command``) runs nothing. A shape the grammar does not
    recognize (a Kiro action list, a malformed entry) is scanned whole, as
    before, so it still fails closed.
    """
    from apm_cli.hook_contract import HookContractError, parse_hook_source

    try:
        commands = [declared.command for declared in parse_hook_source(document).commands]
    except HookContractError:
        commands = list(_every_command_field(document))
    # Raw text: the matcher reads backslash escapes; only the tail match normalizes (#5106).
    return commands


def _every_command_field(node: Any) -> Iterator[str]:
    """Every hook command field anywhere in a document the grammar cannot read."""
    from apm_cli.hook_contract import HOOK_COMMAND_KEYS

    if isinstance(node, dict):
        for key, value in node.items():
            if key in HOOK_COMMAND_KEYS and isinstance(value, str):
                yield value
            else:
                yield from _every_command_field(value)
    elif isinstance(node, list):
        for item in node:
            yield from _every_command_field(item)
