"""Marketplace output mappers.

Mappers translate resolved marketplace packages into each output format's JSON
shape.  ``MarketplaceBuilder`` owns resolution, paths, writing, and diffing;
these classes own format-specific field mapping.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .diagnostics import BuildDiagnostic
from .errors import BuildError

_RE_NON_ALNUM = re.compile(r"[^a-z0-9]")
_RE_MULTI_DASH = re.compile(r"-{2,}")


def sanitize_marketplace_name(name: str) -> str:
    """Normalize a marketplace name to kebab-case for Copilot App compatibility.

    Marketplace names like ``my.marketplace`` or ``My_Package`` are valid
    identifiers on GitHub but rejected by the Copilot App which requires
    kebab-case (lowercase letters, digits, and hyphens only).  This is an
    output-boundary normalization -- the original name is preserved for
    internal lookups and display purposes.

    The conversion lowercases the input, replaces every non-alphanumeric
    character with a hyphen, collapses consecutive hyphens, and strips
    leading/trailing hyphens.

    Note: this is a *display/identity* sanitizer for the emitted JSON only.
    It is distinct from ``client._sanitize_cache_name`` (path-safety for
    on-disk cache keys); do not use this helper for filesystem paths.  The
    mapping is intentionally many-to-one (``my.pkg`` and ``my_pkg`` both map
    to ``my-pkg``); do not use it for identity disambiguation or registry-key
    deduplication.
    """
    result = name.lower()
    result = _RE_NON_ALNUM.sub("-", result)
    result = _RE_MULTI_DASH.sub("-", result)
    result = result.strip("-")
    return result or "marketplace"


def _sanitized_name_with_diagnostic(config_name: str) -> tuple[str, list[BuildDiagnostic]]:
    """Return the sanitized marketplace name and any diagnostic it warrants.

    When the configured name is already kebab-case the diagnostic list is
    empty.  When sanitisation actually rewrites the name, a single
    ``warning``-level ``BuildDiagnostic`` is emitted so the user sees that the
    value landing in ``marketplace.json`` differs from what they configured and
    can rename it in their marketplace config to silence it.  Shared by both
    output mappers so the message and level stay consistent.
    """
    sanitized = sanitize_marketplace_name(config_name)
    diagnostics: list[BuildDiagnostic] = []
    if sanitized != config_name:
        diagnostics.append(
            BuildDiagnostic(
                level="warning",
                message=(
                    f"[!] Marketplace name '{config_name}' is not kebab-case -- "
                    f"emitted as '{sanitized}' in marketplace.json for Copilot App "
                    f"compatibility. Rename it in your marketplace config to silence this."
                ),
            )
        )
    return sanitized, diagnostics


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
        sanitized_name, name_diagnostics = _sanitized_name_with_diagnostic(config.name)
        doc["name"] = sanitized_name
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
        diagnostics: list[BuildDiagnostic] = list(name_diagnostics)
        plugins: list[dict[str, Any]] = []

        for pkg in resolved:
            entry = entry_by_name.get(pkg.name)
            is_local = bool(entry and entry.is_local)
            plugin: dict[str, Any] = OrderedDict()
            plugin["name"] = pkg.name

            meta = remote_metadata.get(pkg.name, {})
            if is_local:
                if _apply_field_with_precedence(
                    plugin,
                    diagnostics,
                    field="description",
                    entry_value=entry.description,
                    meta_value=meta.get("description"),
                    pkg_name=pkg.name,
                    source_label="package apm.yml",
                ):
                    override_count += 1
                if _apply_field_with_precedence(
                    plugin,
                    diagnostics,
                    field="version",
                    entry_value=entry.version,
                    meta_value=meta.get("version"),
                    pkg_name=pkg.name,
                    source_label="package apm.yml",
                ):
                    override_count += 1
            else:
                entry_description = entry.description if entry else None
                entry_version = (
                    entry.version if entry and _is_display_version(entry.version) else None
                )
                if _apply_field_with_precedence(
                    plugin,
                    diagnostics,
                    field="description",
                    entry_value=entry_description,
                    meta_value=meta.get("description"),
                    pkg_name=pkg.name,
                    source_label="remote",
                ):
                    override_count += 1
                if _apply_field_with_precedence(
                    plugin,
                    diagnostics,
                    field="version",
                    entry_value=entry_version,
                    meta_value=meta.get("version"),
                    pkg_name=pkg.name,
                    source_label="remote",
                ):
                    override_count += 1

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
                # Remote source: emit per the official Claude Code marketplace
                # schema. When the package was authored with a host-prefixed
                # source (``host.tld/owner/repo``), emit a real ``https://``
                # URL so Claude Code can clone from a non-default host (e.g.
                # GHE) -- the ``github`` shorthand only resolves to github.com.
                source_obj: dict[str, Any] = OrderedDict()
                remote_url = _remote_source_url(pkg)
                if pkg.subdir:
                    source_obj["source"] = "git-subdir"
                    source_obj["url"] = remote_url or pkg.source_repo
                    source_obj["path"] = pkg.subdir
                elif remote_url:
                    source_obj["source"] = "url"
                    source_obj["url"] = remote_url
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
            field_label = "field override" if override_count == 1 else "field overrides"
            summary_parts.append(
                f"{override_count} curator-supplied {field_label} kept over package metadata"
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
        sanitized, name_diagnostics = _sanitized_name_with_diagnostic(config.name)
        doc["name"] = sanitized
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
        return MapperResult(doc, (), tuple(name_diagnostics))


MARKETPLACE_OUTPUT_MAPPERS: dict[str, MarketplaceOutputMapper] = {
    "claude": ClaudeMarketplaceMapper(),
    "codex": CodexMarketplaceMapper(),
}


def _remote_source_url(pkg: ResolvedPackage) -> str | None:
    """Return the canonical URL for remote packages that cannot use github shorthand."""
    if pkg.source_url:
        return pkg.source_url
    if pkg.host:
        return f"https://{pkg.host}/{pkg.source_repo}"
    return None


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
        source_obj["url"] = _remote_source_url(pkg) or pkg.source_repo
        source_obj["path"] = pkg.subdir
        if pkg.ref:
            source_obj["ref"] = pkg.ref
        if pkg.sha:
            source_obj["sha"] = pkg.sha
        return source_obj

    source_obj = OrderedDict()
    source_obj["source"] = "url"
    source_obj["url"] = _remote_source_url(pkg) or pkg.source_repo
    if pkg.ref:
        source_obj["ref"] = pkg.ref
    if pkg.sha:
        source_obj["sha"] = pkg.sha
    return source_obj


def _apply_field_with_precedence(
    plugin: dict[str, Any],
    diagnostics: list[BuildDiagnostic],
    *,
    field: str,
    entry_value: str | None,
    meta_value: Any,
    pkg_name: str,
    source_label: str,
) -> bool:
    """Apply curator-wins metadata precedence and report override diagnostics."""
    meta_text = meta_value if isinstance(meta_value, str) else ""
    if entry_value:
        plugin[field] = entry_value
        if meta_text and meta_text != entry_value:
            diagnostics.append(
                BuildDiagnostic(
                    level="verbose",
                    message=_override_message(
                        pkg_name=pkg_name,
                        field=field,
                        entry_value=entry_value,
                        source_label=source_label,
                        meta_value=meta_text,
                    ),
                )
            )
            return True
    elif meta_text:
        plugin[field] = meta_text
    return False


def _override_message(
    *,
    pkg_name: str,
    field: str,
    entry_value: str,
    source_label: str,
    meta_value: str,
) -> str:
    """Build a compact verbose diagnostic for curator override choices."""
    field_detail = f"{field} '{entry_value}'" if field == "version" else field
    return (
        f"[i] Package '{pkg_name}': using curator {field_detail} "
        f"({source_label}: '{_diagnostic_preview(meta_value)}')"
    )


def _diagnostic_preview(value: str, *, limit: int = 40) -> str:
    """Return a compact diagnostic preview with an explicit truncation marker."""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


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
