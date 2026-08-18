"""Compatibility facade for canonical installed Agent Plugin roots."""

from __future__ import annotations

import os
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..agent_plugins.loader import load_agent_plugin
from .agent_plugin_state import (
    PreparedAgentPluginRoot,
    prepare_agent_plugin_root,
)


def stable_agent_plugin_id(bundle_info: Any) -> str:
    """Return the storage key derived only from canonical manifest identity."""
    from ..deps.lockfile import InstalledPluginRecordCodec

    plugin = load_agent_plugin(Path(bundle_info.source_dir))
    return InstalledPluginRecordCodec.storage_key(plugin.identity.name)


def _replace_bundle_info(bundle_info: Any, **updates: Any) -> Any:
    """Return *bundle_info* with selected fields replaced."""
    if is_dataclass(bundle_info):
        return replace(bundle_info, **updates)
    values = dict(getattr(bundle_info, "__dict__", {}))
    values.update(updates)
    try:
        return bundle_info.__class__(**values)
    except (TypeError, ValueError):
        return SimpleNamespace(**values)


def stage_agent_plugin_bundle(
    bundle_info: Any,
    project_root: Path,
    *,
    global_: bool,
) -> Any:
    """Copy an Agent Plugin to an owned staging root without activating it."""
    if getattr(bundle_info, "retained_root", None) is not None:
        return bundle_info

    source_dir = Path(bundle_info.source_dir)
    plugin = load_agent_plugin(source_dir)
    transaction = prepare_agent_plugin_root(
        source_dir,
        project_root,
        global_=global_,
        identity=plugin.identity,
    )
    return _replace_bundle_info(
        bundle_info,
        source_dir=transaction.staging_root,
        retained_root=transaction.plugin_root,
        data_root=transaction.data_root,
    )


def commit_agent_plugin_bundle(bundle_info: Any) -> Any:
    """Atomically activate a staged Agent Plugin and preserve plugin data."""
    staging_root = Path(bundle_info.source_dir)
    source_root = Path(bundle_info.retained_root)
    data_root = Path(bundle_info.data_root)
    transaction = PreparedAgentPluginRoot(
        staging_root=staging_root,
        state_base=Path(os.path.commonpath((source_root, data_root))),
        plugin_root=source_root,
        data_root=data_root,
        backup_root=source_root.parent / f".{source_root.name}.previous",
        had_existing_root=source_root.exists(),
    )
    transaction.commit()
    transaction.finalize()
    return _replace_bundle_info(bundle_info, source_dir=source_root)


def discard_staged_agent_plugin_bundle(bundle_info: Any) -> None:
    """Remove an uncommitted Agent Plugin staging root."""
    source_dir = Path(bundle_info.source_dir)
    retained_root = Path(bundle_info.retained_root)
    if source_dir != retained_root and source_dir.exists():
        from ..utils.path_security import safe_rmtree

        safe_rmtree(source_dir, retained_root.parent)


def materialize_agent_plugin_bundle(
    bundle_info: Any,
    project_root: Path,
    *,
    global_: bool,
) -> Any:
    """Compatibility facade that stages and immediately commits a bundle."""
    staged = stage_agent_plugin_bundle(bundle_info, project_root, global_=global_)
    return commit_agent_plugin_bundle(staged)
