"""Read an explicitly selected local authoring catalog without producing files."""

from __future__ import annotations

from pathlib import Path

import click

from .builder import BuildOptions, MarketplaceBuilder
from .errors import MarketplaceError, MarketplaceFetchError, OfflineMissError
from .models import MarketplaceSource, parse_marketplace_json
from .resolver import resolve_local_plugin_path
from .yml_schema import load_marketplace_from_apm_yml


def load_authoring_catalog(
    source: MarketplaceSource,
    *,
    offline: bool = False,
    auth_resolver: object | None = None,
) -> dict:
    """Resolve and compose one snapshot per Click invocation, rereading next time.

    Only the user-selected local ``apm.yml`` enters this path. Native output
    mappers still own the catalog shape; no packing or writing is involved.
    Remote refs use the normal builder resolver. Cosmetic remote metadata is
    deliberately omitted so browsing does not download every package manifest.
    """
    path = Path(source.local_path).expanduser().absolute()
    ctx = click.get_current_context(silent=True)
    snapshots = ctx.find_root().meta.setdefault(__name__, {}) if ctx else {}
    key = (str(path), offline)
    if key in snapshots:
        return snapshots[key]

    try:
        config = load_marketplace_from_apm_yml(path)
        builder = MarketplaceBuilder.from_config(
            config,
            path.parent,
            options=BuildOptions(offline=offline),
            auth_resolver=auth_resolver,
        )
        # Authored paths are relative to the file. Validate them through the
        # existing containment owner before resolving or composing the catalog.
        for entry in config.packages:
            if entry.is_local:
                resolve_local_plugin_path(entry.source, source)
        resolved = builder.resolve()
        document = builder.compose_discovery_catalog(resolved.entries)
        manifest = parse_marketplace_json(document, source.name)
        # Validate the composed consumer interpretation through the same owner;
        # native output layout hints must not change these author-relative paths.
        for plugin in manifest.plugins:
            if isinstance(plugin.source, str):
                resolve_local_plugin_path(plugin.source, source, manifest.plugin_root)
        if manifest.structural_errors:
            raise ValueError("; ".join(manifest.structural_errors))
    except (MarketplaceError, OSError, ValueError) as exc:
        retry_hint = (
            "Retry without --offline to resolve remote refs, or pin remote entries to full commit SHAs."
            if isinstance(exc, OfflineMissError)
            else "Check the marketplace section, package paths and refs in that file, then retry."
        )
        raise MarketplaceFetchError(
            source.name,
            f"cannot load marketplace declaration from {path}: {exc}",
            retry_hint=retry_hint,
        ) from exc
    snapshots[key] = document
    return document
