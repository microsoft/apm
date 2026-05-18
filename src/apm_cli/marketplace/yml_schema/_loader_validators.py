"""Validator helpers and config builder for marketplace YAML loading.

Extracted from :mod:`loaders` to keep that module under 400 lines.
All public names continue to be importable from
:mod:`apm_cli.marketplace.yml_schema.loaders` via explicit re-exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...utils.path_security import PathTraversalError, validate_path_segments
from ..errors import MarketplaceYmlError
from ..output_profiles import MARKETPLACE_OUTPUTS
from .class_ import MarketplaceConfig, MarketplaceOutputSpec, PackageEntry
from .parse_helpers import (
    _parse_build,
    _parse_claude,
    _parse_codex,
    _parse_outputs,
    _parse_owner,
    _parse_package_entry,
    _parse_versioning,
)


@dataclass
class _BuildConfigInput:
    """Input parameters for :func:`_build_config`."""

    marketplace_dict: dict[str, Any]
    name: str
    description: str
    version: str
    source_path: Path
    is_legacy: bool
    name_overridden: bool
    description_overridden: bool
    version_overridden: bool
    default_output: str = ".claude-plugin/marketplace.json"


def _validate_and_parse_owner(marketplace_dict: dict) -> OwnerInfo:
    """Validate and parse owner field."""
    raw_owner = marketplace_dict.get("owner")
    if raw_owner is None:
        raise MarketplaceYmlError("'owner' is required")
    return _parse_owner(raw_owner)


def _validate_and_parse_output(
    marketplace_dict: dict,
    default_output: str,
) -> tuple[str, ClaudeOutputInfo]:
    """Validate output path and parse Claude block."""
    legacy_output = marketplace_dict.get("output")
    output = default_output if legacy_output is None else legacy_output
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'output' must be a non-empty string")
    output = output.strip()

    # Path-traversal guard
    try:
        validate_path_segments(output, context="marketplace output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    claude = _parse_claude(marketplace_dict.get("claude"), default_output=output)
    return (claude.output, claude)


def _validate_metadata_and_plugin_root(marketplace_dict: dict) -> dict[str, Any]:
    """Validate metadata and pluginRoot path safety."""
    metadata: dict[str, Any] = {}
    raw_metadata = marketplace_dict.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict):
            raise MarketplaceYmlError("'metadata' must be a mapping")
        metadata = dict(raw_metadata)

    plugin_root = metadata.get("pluginRoot")
    if plugin_root is not None and isinstance(plugin_root, str) and plugin_root.strip():
        try:
            validate_path_segments(
                plugin_root.strip(),
                context="metadata.pluginRoot",
                allow_current_dir=True,
            )
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc

    return metadata


def _resolve_output_conflicts(
    marketplace_dict: dict,
    output_specs: tuple[MarketplaceOutputSpec, ...],
    claude: ClaudeOutputInfo,
    codex: CodexOutputInfo,
    warnings_sink: list[str],
) -> tuple[MarketplaceOutputSpec, ...]:
    """Resolve sibling-vs-map output path conflicts."""
    has_explicit_claude = marketplace_dict.get("claude") is not None
    has_explicit_codex = marketplace_dict.get("codex") is not None

    final_specs_list = list(output_specs)
    for i, spec in enumerate(final_specs_list):
        if not spec.path_explicit:
            continue

        sibling_path: str | None = None
        if spec.name == "claude" and has_explicit_claude and claude.output != spec.path:
            sibling_path = claude.output
        elif spec.name == "codex" and has_explicit_codex and codex.output != spec.path:
            sibling_path = codex.output

        if sibling_path is not None:
            warnings_sink.append(
                f"marketplace.outputs.{spec.name}.path ('{spec.path}') "
                f"conflicts with marketplace.{spec.name}.output "
                f"('{sibling_path}').\n"
                f"    Using marketplace.{spec.name}.output for backwards "
                f"compatibility.\n\n"
                f"    To resolve: pick one source and remove the other.\n"
                f"      Keep map form (recommended):\n"
                f"        outputs:\n"
                f"          {spec.name}:\n"
                f"            path: {sibling_path}\n"
                f"        # remove the marketplace.{spec.name}: block\n\n"
                f"    The marketplace.{spec.name} sibling block becomes a "
                f"schema error in v0.15."
            )
            # Sibling wins: override the spec's path
            final_specs_list[i] = MarketplaceOutputSpec(
                name=spec.name,
                path=sibling_path,
                path_explicit=True,
            )
    return tuple(final_specs_list)


def _parse_and_validate_packages(
    marketplace_dict: dict,
    outputs: tuple[str, ...],
) -> tuple[PackageEntry, ...]:
    """Parse and validate packages list with duplicate and required field checks."""
    raw_packages = marketplace_dict.get("packages")
    if raw_packages is None:
        raw_packages = []
    if not isinstance(raw_packages, list):
        raise MarketplaceYmlError("'packages' must be a list")

    entries: list[PackageEntry] = []
    seen_names: dict[str, int] = {}
    for idx, raw_entry in enumerate(raw_packages):
        entry = _parse_package_entry(raw_entry, idx)
        lower_name = entry.name.lower()
        if lower_name in seen_names:
            raise MarketplaceYmlError(
                f"Duplicate package name '{entry.name}' "
                f"(packages[{seen_names[lower_name]}] and packages[{idx}])"
            )
        seen_names[lower_name] = idx
        entries.append(entry)

    for output_name in outputs:
        profile = MARKETPLACE_OUTPUTS[output_name]
        for field_name in profile.required_package_fields:
            missing = [entry.name for entry in entries if not getattr(entry, field_name)]
            if missing:
                names = ", ".join(missing)
                raise MarketplaceYmlError(
                    f"packages must define '{field_name}' when marketplace.outputs includes "
                    f"'{output_name}' (missing: {names})"
                )

    return tuple(entries)


def _build_config(ctx: _BuildConfigInput) -> MarketplaceConfig:
    """Shared parser for the marketplace fields once name/desc/version
    have been resolved (either inherited or read directly).
    """
    marketplace_dict = ctx.marketplace_dict
    warnings_sink: list[str] = []

    owner = _validate_and_parse_owner(marketplace_dict)

    outputs, output_specs = _parse_outputs(
        marketplace_dict.get("outputs"), warnings_sink=warnings_sink
    )

    output, claude = _validate_and_parse_output(
        marketplace_dict,
        ctx.default_output,
    )

    metadata = _validate_metadata_and_plugin_root(marketplace_dict)

    build = _parse_build(marketplace_dict.get("build"))
    codex = _parse_codex(marketplace_dict.get("codex"))
    versioning = _parse_versioning(marketplace_dict.get("versioning"))

    output_specs = _resolve_output_conflicts(
        marketplace_dict,
        output_specs,
        claude,
        codex,
        warnings_sink,
    )

    entries = _parse_and_validate_packages(marketplace_dict, outputs)

    return MarketplaceConfig(
        name=ctx.name,
        description=ctx.description,
        version=ctx.version,
        owner=owner,
        output=output,
        outputs=outputs,
        claude=claude,
        codex=codex,
        metadata=metadata,
        build=build,
        versioning=versioning,
        packages=entries,
        output_specs=output_specs,
        warnings=tuple(warnings_sink),
        source_path=ctx.source_path,
        is_legacy=ctx.is_legacy,
        name_overridden=ctx.name_overridden,
        description_overridden=ctx.description_overridden,
        version_overridden=ctx.version_overridden,
    )
