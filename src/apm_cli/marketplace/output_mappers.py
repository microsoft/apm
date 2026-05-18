"""Marketplace output mappers.

Mappers translate resolved marketplace packages into each output format's JSON
shape.  ``MarketplaceBuilder`` owns resolution, paths, writing, and diffing;
these classes own format-specific field mapping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .diagnostics import BuildDiagnostic
from .errors import BuildError

if TYPE_CHECKING:
    from .builder import ResolvedPackage
    from .yml_schema import MarketplaceConfig, PackageEntry


@dataclass(frozen=True)
class MapperResult:
    """Composed output plus mapper diagnostics."""

    document: dict[str, Any]
    warnings: tuple[str, ...] = ()
    diagnostics: tuple[BuildDiagnostic, ...] = ()


class MarketplaceOutputMapper(ABC):
    """Base class for marketplace output format mappers."""

    uses_remote_metadata = False

    @abstractmethod
    def compose(
        self,
        *,
        config: MarketplaceConfig,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        """Return the output JSON document for resolved packages."""


def _build_claude_document(config: MarketplaceConfig) -> dict[str, Any]:
    """Build the top-level Claude marketplace document."""
    doc: dict[str, Any] = OrderedDict()
    doc["name"] = config.name
    if config.description_overridden and config.description:
        doc["description"] = config.description
    if config.version_overridden and config.version:
        doc["version"] = config.version
    owner_dict: dict[str, Any] = OrderedDict()
    owner_dict["name"] = config.owner.name
    if config.owner.email:
        owner_dict["email"] = config.owner.email
    if config.owner.url:
        owner_dict["url"] = config.owner.url
    doc["owner"] = owner_dict
    if config.metadata:
        doc["metadata"] = config.metadata
    return doc


class ClaudeMarketplaceMapper(MarketplaceOutputMapper):
    """Map packages into Claude/Anthropic marketplace.json format."""

    uses_remote_metadata = True

    def compose(
        self,
        *,
        config: MarketplaceConfig,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        remote_metadata = remote_metadata or {}
        entry_by_name: dict[str, PackageEntry] = {e.name: e for e in config.packages}
        doc = _build_claude_document(config)
        plugin_root = config.metadata.get("pluginRoot", "")
        strip_count = 0
        override_count = 0
        diagnostics: list[BuildDiagnostic] = []
        plugins: list[dict[str, Any]] = []

        for pkg in resolved:
            entry = entry_by_name.get(pkg.name)
            plugin, plugin_strip_count, plugin_override_count, plugin_diagnostics = (
                _map_claude_plugin(
                    entry=entry,
                    pkg=pkg,
                    plugin_root=plugin_root,
                    remote_metadata=remote_metadata.get(pkg.name, {}),
                )
            )
            plugins.append(plugin)
            strip_count += plugin_strip_count
            override_count += plugin_override_count
            diagnostics.extend(plugin_diagnostics)

        _append_claude_summary(
            diagnostics=diagnostics,
            plugin_root=plugin_root,
            strip_count=strip_count,
            override_count=override_count,
        )
        warnings = _duplicate_name_warnings(plugins)
        doc["plugins"] = plugins
        return MapperResult(doc, tuple(warnings), tuple(diagnostics))


def _map_claude_plugin(
    *,
    entry: PackageEntry | None,
    pkg: ResolvedPackage,
    plugin_root: str,
    remote_metadata: dict[str, Any],
) -> tuple[dict[str, Any], int, int, list[BuildDiagnostic]]:
    """Map one resolved package into a Claude marketplace plugin entry."""
    plugin: dict[str, Any] = OrderedDict()
    plugin["name"] = pkg.name
    diagnostics: list[BuildDiagnostic] = []
    strip_count = 0
    override_count = 0
    is_local = bool(entry and entry.is_local)

    if is_local:
        _apply_claude_local_metadata(plugin, entry)
    else:
        override_count += _apply_claude_remote_metadata(
            plugin=plugin,
            entry=entry,
            pkg=pkg,
            remote_metadata=remote_metadata,
            diagnostics=diagnostics,
        )

    _apply_claude_shared_metadata(plugin=plugin, entry=entry, pkg=pkg, is_local=is_local)
    source_value, source_strip_count, source_diagnostics = _build_claude_source(
        entry=entry,
        pkg=pkg,
        plugin_root=plugin_root,
        is_local=is_local,
    )
    strip_count += source_strip_count
    diagnostics.extend(source_diagnostics)
    plugin["source"] = source_value
    return plugin, strip_count, override_count, diagnostics


def _apply_claude_local_metadata(plugin: dict[str, Any], entry: PackageEntry) -> None:
    """Apply local-only description and version metadata."""
    if entry.description:
        plugin["description"] = entry.description
    if entry.version:
        plugin["version"] = entry.version


def _apply_claude_remote_metadata(
    *,
    plugin: dict[str, Any],
    entry: PackageEntry | None,
    pkg: ResolvedPackage,
    remote_metadata: dict[str, Any],
    diagnostics: list[BuildDiagnostic],
) -> int:
    """Apply remote metadata and return the number of curator overrides used."""
    override_count = 0
    remote_desc = remote_metadata.get("description", "")
    if entry and entry.description:
        plugin["description"] = entry.description
        if remote_desc and remote_desc != entry.description:
            override_count += 1
            diagnostics.append(
                BuildDiagnostic(
                    level="verbose",
                    message=(
                        f"[i] Package '{pkg.name}': using curator description "
                        f"(remote: '{remote_desc[:40]}')"
                    ),
                )
            )
    elif remote_desc:
        plugin["description"] = remote_desc

    remote_ver = remote_metadata.get("version", "")
    if entry and _is_display_version(entry.version):
        plugin["version"] = entry.version
        if remote_ver and remote_ver != entry.version:
            override_count += 1
            diagnostics.append(
                BuildDiagnostic(
                    level="verbose",
                    message=(
                        f"[i] Package '{pkg.name}': using curator version '{entry.version}' "
                        f"(remote: '{remote_ver}')"
                    ),
                )
            )
    elif remote_ver:
        plugin["version"] = remote_ver
    return override_count


def _apply_claude_shared_metadata(
    *, plugin: dict[str, Any], entry: PackageEntry | None, pkg: ResolvedPackage, is_local: bool
) -> None:
    """Apply metadata shared by local and remote plugin entries."""
    if entry and entry.author:
        plugin["author"] = dict(entry.author)
    if entry and entry.license:
        plugin["license"] = entry.license
    if entry and entry.repository:
        plugin["repository"] = entry.repository
    if pkg.tags:
        plugin["tags"] = list(pkg.tags)
    if is_local and entry and entry.homepage:
        plugin["homepage"] = entry.homepage


def _build_claude_source(
    *, entry: PackageEntry | None, pkg: ResolvedPackage, plugin_root: str, is_local: bool
) -> tuple[str | dict[str, Any], int, list[BuildDiagnostic]]:
    """Build the Claude source field and related diagnostics."""
    if is_local and entry is not None:
        return _build_claude_local_source(entry=entry, pkg=pkg, plugin_root=plugin_root)
    return _build_claude_remote_source(pkg), 0, []


def _build_claude_local_source(
    *, entry: PackageEntry, pkg: ResolvedPackage, plugin_root: str
) -> tuple[str, int, list[BuildDiagnostic]]:
    """Build the Claude local source value."""
    del pkg
    diagnostics: list[BuildDiagnostic] = []
    if not plugin_root:
        return entry.source, 0, diagnostics
    try:
        source_value = _subtract_plugin_root(entry.source, plugin_root)
        diagnostics.append(
            BuildDiagnostic(
                level="verbose",
                message=(
                    f"[i] Package '{entry.name}': stripped pluginRoot -- "
                    f"'{entry.source}' -> '{source_value}'"
                ),
            )
        )
        return source_value, 1, diagnostics
    except ValueError:
        diagnostics.append(
            BuildDiagnostic(
                level="warning",
                message=(
                    f"[!] Package '{entry.name}': source '{entry.source}' is outside "
                    f"pluginRoot '{plugin_root}' -- emitted as-is"
                ),
            )
        )
        return entry.source, 0, diagnostics


def _build_claude_remote_source(pkg: ResolvedPackage) -> dict[str, Any]:
    """Build the Claude remote source object."""
    source_obj: dict[str, Any] = OrderedDict()
    if pkg.subdir:
        source_obj["source"] = "git-subdir"
        source_obj["url"] = pkg.source_repo
        source_obj["path"] = pkg.subdir
    else:
        source_obj["source"] = "github"
        source_obj["repo"] = pkg.source_repo
    if pkg.ref:
        source_obj["ref"] = pkg.ref
    if pkg.sha:
        source_obj["sha"] = pkg.sha
    return source_obj


def _append_claude_summary(
    *,
    diagnostics: list[BuildDiagnostic],
    plugin_root: str,
    strip_count: int,
    override_count: int,
) -> None:
    """Append pluginRoot summary diagnostics when relevant."""
    summary_parts: list[str] = []
    if plugin_root and strip_count > 0:
        summary_parts.append(f"stripped from {strip_count} local source(s)")
    if override_count > 0:
        summary_parts.append(f"{override_count} remote entry(ies) used curator-supplied overrides")
    if summary_parts:
        diagnostics.append(
            BuildDiagnostic(
                level="verbose",
                message="pluginRoot: " + "; ".join(summary_parts),
            )
        )


class CodexMarketplaceMapper(MarketplaceOutputMapper):
    """Map packages into Codex repo marketplace format."""

    def compose(
        self,
        *,
        config: MarketplaceConfig,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        entry_by_name: dict[str, PackageEntry] = {e.name: e for e in config.packages}

        doc: dict[str, Any] = OrderedDict()
        doc["name"] = config.name
        doc["interface"] = OrderedDict({"displayName": config.name})

        plugins: list[dict[str, Any]] = []
        for pkg in resolved:
            entry = entry_by_name.get(pkg.name)
            if entry is None:
                continue

            plugin: dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name
            plugin["source"] = _codex_source(entry, pkg)
            plugin["policy"] = OrderedDict(
                {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                }
            )
            if not entry.category:
                raise BuildError(
                    f"package '{entry.name}' is missing category required for Codex output"
                )
            plugin["category"] = entry.category
            plugins.append(plugin)

        doc["plugins"] = plugins
        return MapperResult(doc)


MARKETPLACE_OUTPUT_MAPPERS: dict[str, MarketplaceOutputMapper] = {
    "claude": ClaudeMarketplaceMapper(),
    "codex": CodexMarketplaceMapper(),
}


def _codex_source(entry: PackageEntry, pkg: ResolvedPackage) -> dict[str, Any]:
    if entry.is_local:
        return OrderedDict(
            {
                "source": "local",
                "path": entry.source,
            }
        )
    if pkg.subdir:
        source_obj: dict[str, Any] = OrderedDict()
        source_obj["source"] = "git-subdir"
        source_obj["url"] = pkg.source_repo
        source_obj["path"] = pkg.subdir
        if pkg.ref:
            source_obj["ref"] = pkg.ref
        if pkg.sha:
            source_obj["sha"] = pkg.sha
        return source_obj

    source_obj = OrderedDict()
    source_obj["source"] = "url"
    source_obj["url"] = pkg.source_repo
    if pkg.ref:
        source_obj["ref"] = pkg.ref
    if pkg.sha:
        source_obj["sha"] = pkg.sha
    return source_obj


from ._mapper_helpers import (
    _duplicate_name_warnings,
    _is_display_version,
    _subtract_plugin_root,
)
