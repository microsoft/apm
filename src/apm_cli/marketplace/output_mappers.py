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

        plugin_root = config.metadata.get("pluginRoot", "")
        strip_count = 0
        override_count = 0
        diagnostics: list[BuildDiagnostic] = []
        plugins: list[dict[str, Any]] = []

        for pkg in resolved:
            entry = entry_by_name.get(pkg.name)
            is_local = bool(entry and entry.is_local)
            plugin: dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name

            if is_local:
                if entry.description:
                    plugin["description"] = entry.description
                if entry.version:
                    plugin["version"] = entry.version
            else:
                meta = remote_metadata.get(pkg.name, {})
                if entry and entry.description:
                    plugin["description"] = entry.description
                    remote_desc = meta.get("description", "")
                    if remote_desc and remote_desc != entry.description:
                        override_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': using curator "
                                    f"description (remote: "
                                    f"'{remote_desc[:40]}')"
                                ),
                            )
                        )
                elif meta.get("description"):
                    plugin["description"] = meta["description"]

                if entry and _is_display_version(entry.version):
                    plugin["version"] = entry.version
                    remote_ver = meta.get("version", "")
                    if remote_ver and remote_ver != entry.version:
                        override_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': using curator "
                                    f"version '{entry.version}' "
                                    f"(remote: '{remote_ver}')"
                                ),
                            )
                        )
                elif meta.get("version"):
                    plugin["version"] = meta["version"]

            if entry and entry.author:
                plugin["author"] = dict(entry.author)
            if entry and entry.license:
                plugin["license"] = entry.license
            if entry and entry.repository:
                plugin["repository"] = entry.repository
            if pkg.tags:
                plugin["tags"] = list(pkg.tags)
            if is_local and entry.homepage:
                plugin["homepage"] = entry.homepage

            if is_local:
                source_value = entry.source
                if plugin_root:
                    try:
                        source_value = _subtract_plugin_root(entry.source, plugin_root)
                        strip_count += 1
                        diagnostics.append(
                            BuildDiagnostic(
                                level="verbose",
                                message=(
                                    f"[i] Package '{pkg.name}': stripped "
                                    f"pluginRoot -- '{entry.source}' -> "
                                    f"'{source_value}'"
                                ),
                            )
                        )
                    except ValueError:
                        source_value = entry.source
                        diagnostics.append(
                            BuildDiagnostic(
                                level="warning",
                                message=(
                                    f"[!] Package '{pkg.name}': source "
                                    f"'{entry.source}' is outside pluginRoot "
                                    f"'{plugin_root}' -- emitted as-is"
                                ),
                            )
                        )
                plugin["source"] = source_value
            else:
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
                plugin["source"] = source_obj

            plugins.append(plugin)

        summary_parts: list[str] = []
        if plugin_root and strip_count > 0:
            summary_parts.append(f"stripped from {strip_count} local source(s)")
        if override_count > 0:
            summary_parts.append(
                f"{override_count} remote entry(ies) used curator-supplied overrides"
            )
        if summary_parts:
            diagnostics.append(
                BuildDiagnostic(
                    level="verbose",
                    message="pluginRoot: " + "; ".join(summary_parts),
                )
            )

        warnings = _duplicate_name_warnings(plugins)
        doc["plugins"] = plugins
        return MapperResult(doc, tuple(warnings), tuple(diagnostics))


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


def _duplicate_name_warnings(plugins: list[dict[str, Any]]) -> list[str]:
    seen_names: dict[str, str] = {}
    warnings: list[str] = []
    for plugin in plugins:
        name = plugin["name"]
        source = plugin.get("source", {})
        if isinstance(source, str):
            source_label = source
        else:
            source_label = source.get("path") or source.get("repo") or source.get("repository", "?")
        if name in seen_names:
            warnings.append(
                f"Duplicate package name '{name}': "
                f"'{seen_names[name]}' and '{source_label}'. "
                f"Consumers will see duplicate entries in browse."
            )
        else:
            seen_names[name] = source_label
    return warnings


def _is_display_version(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    if any(stripped.startswith(char) for char in ("^", "~", ">", "<", "=")):
        return False
    return not (" " in stripped or "*" in stripped or "x" in stripped.lower().split(".")[-1:])


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    from pathlib import PurePosixPath

    norm_source = source.lstrip("./") if source.startswith("./") else source
    norm_root = plugin_root.lstrip("./") if plugin_root.startswith("./") else plugin_root
    norm_root = norm_root.rstrip("/")
    norm_source = norm_source.rstrip("/")

    src_path = PurePosixPath(norm_source)
    root_path = PurePosixPath(norm_root)

    relative = src_path.relative_to(root_path)
    result = str(relative)

    if not result or result == ".":
        raise BuildError(
            f"subtracting pluginRoot '{plugin_root}' from source '{source}' yields empty path"
        )

    if result.startswith("/"):
        raise BuildError(f"pluginRoot subtraction produced absolute path: '{result}'")
    if ".." in result.split("/"):
        raise BuildError(f"pluginRoot subtraction produced path with traversal: '{result}'")

    return "./" + result
