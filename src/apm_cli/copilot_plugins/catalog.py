"""Deterministic rendering of the APM-owned Copilot directory marketplace.

APM generates exactly one aggregate catalog per materialization root:
``<apm_modules>/.github/plugin/marketplace.json``. Every entry points at the
real dependency directory with a relative string source, so Copilot loads the
plugin live from APM's own bytes and never copies it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..agent_plugins.ir import AgentPlugin
from .constants import APM_MARKETPLACE_NAME, APM_MARKETPLACE_OWNER


@dataclass(frozen=True, slots=True)
class NativePluginEntry:
    """One Agent Plugin admitted into the APM-owned catalog."""

    dependency_key: str
    plugin_name: str
    version: str | None
    description: str | None
    source: str
    root: Path
    direct: bool = False

    @property
    def enabled_key(self) -> str:
        """Return the ``<plugin>@<marketplace>`` key Copilot enables."""
        return f"{self.plugin_name}@{APM_MARKETPLACE_NAME}"


class CatalogSourceError(ValueError):
    """Raised when a plugin root cannot be expressed inside the catalog."""


def relative_plugin_source(marketplace_root: Path, plugin_root: Path) -> str:
    """Return the deterministic ``./``-prefixed relative source for a plugin.

    The source stays relative so a project catalog is portable across clones,
    worktrees, and checkout roots.
    """
    from ..utils.paths import portable_relpath

    try:
        plugin_root.resolve().relative_to(marketplace_root.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        raise CatalogSourceError(
            f"Agent Plugin root {plugin_root} is not inside {marketplace_root}"
        ) from exc
    posix = PurePosixPath(portable_relpath(plugin_root, marketplace_root)).as_posix()
    if not posix or posix == ".":
        raise CatalogSourceError(
            f"Agent Plugin root {plugin_root} must not be the marketplace root"
        )
    return f"./{posix}"


def build_entry(
    *,
    dependency_key: str,
    plugin: AgentPlugin,
    marketplace_root: Path,
    direct: bool = False,
) -> NativePluginEntry:
    """Build one catalog entry from canonical Agent Plugin IR."""
    identity = plugin.identity
    return NativePluginEntry(
        dependency_key=dependency_key,
        plugin_name=identity.name,
        version=identity.version,
        description=identity.description,
        source=relative_plugin_source(marketplace_root, plugin.root),
        root=plugin.root,
        direct=direct,
    )


def order_entries(entries: list[NativePluginEntry]) -> list[NativePluginEntry]:
    """Return entries in a deterministic, host-independent order."""
    return sorted(entries, key=lambda entry: (entry.plugin_name, entry.source))


def render_catalog(entries: list[NativePluginEntry]) -> str:
    """Render the APM-owned ``marketplace.json`` document."""
    document = {
        "name": APM_MARKETPLACE_NAME,
        "owner": dict(APM_MARKETPLACE_OWNER),
        "description": "APM-managed Agent Plugins materialized under apm_modules.",
        "plugins": [_render_entry(entry) for entry in order_entries(entries)],
    }
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def _render_entry(entry: NativePluginEntry) -> dict[str, str]:
    """Render one catalog plugin record, omitting absent optional fields."""
    record: dict[str, str] = {"name": entry.plugin_name, "source": entry.source}
    if entry.version:
        record["version"] = entry.version
    if entry.description:
        record["description"] = entry.description
    return record
