"""Dataclasses, loader, and validation for marketplace authoring config.

The marketplace publisher configuration may live in two places:

* (Preferred, current) inside ``apm.yml`` under a top-level
  ``marketplace:`` block.  Loaded via
  :func:`load_marketplace_from_apm_yml`.
* (Legacy, deprecated) inside a standalone ``marketplace.yml`` file.
  Loaded via :func:`load_marketplace_from_legacy_yml`.

Both paths produce the same immutable :class:`MarketplaceConfig`
dataclass that the builder consumes.

Key design rules
----------------
* **Anthropic pass-through preservation.**  The ``metadata`` block is
  stored as a plain ``dict`` with original key casing (e.g.
  ``pluginRoot`` stays ``pluginRoot``).  Unknown keys inside ``metadata``
  are preserved -- only the builder decides what is forwarded.
* **APM-only vs Anthropic separation.**  Build-time fields (``build``,
  ``version``, ``ref``, ``subdir``, ``tag_pattern``,
  ``include_prerelease``) live as explicit dataclass attributes so the
  builder can strip them cleanly.
* **Strict key sets.**  Unknown keys inside the marketplace block raise
  ``MarketplaceYmlError`` so typos are never silently ignored.  The
  apm.yml top-level is intentionally NOT strict here -- only the
  ``marketplace:`` subtree is validated by this module.
* **Local-path packages.**  ``source`` accepts ``./...`` paths in
  addition to ``owner/repo`` shape.  Local packages skip ref resolution.

Internal implementation is split across sibling leaf modules to keep
complexity manageable:

* ``._yml_models`` -- frozen dataclasses (no parse logic)
* ``._yml_parsers`` -- constants, validators, parse helpers, YAML reader
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils.path_security import PathTraversalError, validate_path_segments

# ---------------------------------------------------------------------------
# Re-export dataclasses from the leaf model module so that existing callers
# such as ``from apm_cli.marketplace.yml_schema import PackageEntry`` keep
# working without any changes.
# ---------------------------------------------------------------------------
from ._yml_models import (
    MarketplaceBuild as MarketplaceBuild,
)
from ._yml_models import (
    MarketplaceClaudeConfig as MarketplaceClaudeConfig,
)
from ._yml_models import (
    MarketplaceCodexConfig as MarketplaceCodexConfig,
)
from ._yml_models import (
    MarketplaceConfig as MarketplaceConfig,
)
from ._yml_models import (
    MarketplaceOutputSpec as MarketplaceOutputSpec,
)
from ._yml_models import (
    MarketplaceOwner as MarketplaceOwner,
)
from ._yml_models import (
    MarketplaceVersioning as MarketplaceVersioning,
)
from ._yml_models import (
    PackageEntry as PackageEntry,
)

# ---------------------------------------------------------------------------
# Re-export parse helpers so test files that do
# ``from apm_cli.marketplace.yml_schema import _parse_author`` keep working.
# The ``X as X`` form signals intentional re-export to ruff (suppresses F401).
# ---------------------------------------------------------------------------
from ._yml_parsers import (
    _APM_MARKETPLACE_KEYS as _APM_MARKETPLACE_KEYS,
)

# ---------------------------------------------------------------------------
# Re-export public parser symbols so callers that import SOURCE_RE /
# LOCAL_SOURCE_RE / split_host_from_source from this module keep working.
# ---------------------------------------------------------------------------
from ._yml_parsers import (
    LOCAL_SOURCE_RE as LOCAL_SOURCE_RE,
)
from ._yml_parsers import (
    SOURCE_BASE_RE as SOURCE_BASE_RE,
)
from ._yml_parsers import (
    SOURCE_RE as SOURCE_RE,
)
from ._yml_parsers import (
    _build_config_fields as _build_config_fields,
)
from ._yml_parsers import (
    _check_unknown_keys as _check_unknown_keys,
)
from ._yml_parsers import (
    _parse_author as _parse_author,
)
from ._yml_parsers import (
    _parse_build as _parse_build,
)
from ._yml_parsers import (
    _parse_claude as _parse_claude,
)
from ._yml_parsers import (
    _parse_codex as _parse_codex,
)
from ._yml_parsers import (
    _parse_outputs as _parse_outputs,
)
from ._yml_parsers import (
    _parse_owner as _parse_owner,
)
from ._yml_parsers import (
    _parse_package_entry as _parse_package_entry,
)
from ._yml_parsers import (
    _parse_versioning as _parse_versioning,
)
from ._yml_parsers import (
    _read_yaml_mapping as _read_yaml_mapping,
)
from ._yml_parsers import (
    _require_str as _require_str,
)
from ._yml_parsers import (
    _validate_semver as _validate_semver,
)
from ._yml_parsers import (
    _validate_source as _validate_source,
)
from ._yml_parsers import (
    _validate_tag_pattern as _validate_tag_pattern,
)
from ._yml_parsers import (
    parse_source_base as parse_source_base,
)
from ._yml_parsers import (
    split_host_from_source as split_host_from_source,
)
from ._yml_parsers import (
    split_source_base as split_source_base,
)
from ._yml_parsers import (
    validate_source_value as validate_source_value,
)
from .errors import MarketplaceYmlError
from .output_profiles import MARKETPLACE_OUTPUTS

__all__ = [
    "LOCAL_SOURCE_RE",
    "SOURCE_BASE_RE",
    "SOURCE_RE",
    "MarketplaceBuild",
    "MarketplaceClaudeConfig",
    "MarketplaceCodexConfig",
    "MarketplaceConfig",
    "MarketplaceOutputSpec",
    "MarketplaceOwner",
    "MarketplaceYml",  # backwards-compat alias
    "MarketplaceYmlError",
    "PackageEntry",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
    "load_marketplace_yml",
    "parse_source_base",
    "split_host_from_source",
    "split_source_base",
    "validate_source_value",
]

# Backwards-compatibility alias for callers that still import ``MarketplaceYml``.
MarketplaceYml = MarketplaceConfig


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_marketplace_yml(path: Path) -> MarketplaceConfig:
    """Backwards-compatible loader for a standalone ``marketplace.yml``.

    Equivalent to :func:`load_marketplace_from_legacy_yml`.  Preserved
    for callers that imported the original symbol.
    """
    return load_marketplace_from_legacy_yml(path)


def load_marketplace_from_legacy_yml(path: Path) -> MarketplaceConfig:
    """Load and validate a standalone ``marketplace.yml`` (legacy).

    The legacy file holds the marketplace block at the YAML root.
    ``name``, ``description``, ``version`` are all required at this
    level (they are not inheritable in the legacy world).

    Parameters
    ----------
    path : Path
        Filesystem path to the YAML file.

    Returns
    -------
    MarketplaceConfig
        Fully validated, immutable representation, with
        ``is_legacy=True`` and all override flags set to ``True`` (the
        legacy file always carries the values explicitly).

    Raises
    ------
    MarketplaceYmlError
        On any validation failure or YAML parse error.
    """
    data = _read_yaml_mapping(path)

    _check_unknown_keys(data, _APM_MARKETPLACE_KEYS, context="top level")

    name = _require_str(data, "name")
    description = _require_str(data, "description")
    version_str = _require_str(data, "version")
    _validate_semver(version_str, context="version")

    return _build_config(
        marketplace_dict=data,
        name=name,
        description=description,
        version=version_str,
        source_path=path,
        is_legacy=True,
        name_overridden=True,
        description_overridden=True,
        version_overridden=True,
        default_output="marketplace.json",
    )


def load_marketplace_from_apm_yml(apm_yml_path: Path) -> MarketplaceConfig:
    """Load marketplace config from apm.yml's ``marketplace:`` block.

    Reads the full YAML, extracts top-level ``name``/``version``/
    ``description``, then parses the ``marketplace:`` block.  Inherits
    the three top-level scalars when the marketplace block does not
    explicitly override them.

    Parameters
    ----------
    apm_yml_path : Path
        Filesystem path to apm.yml.

    Returns
    -------
    MarketplaceConfig
        Fully validated, immutable representation.

    Raises
    ------
    MarketplaceYmlError
        If apm.yml is missing the ``marketplace:`` block or any
        validation fails.
    """
    data = _read_yaml_mapping(apm_yml_path)

    raw_block = data.get("marketplace")
    if raw_block is None:
        raise MarketplaceYmlError(
            f"'{apm_yml_path}' has no 'marketplace:' block. "
            "Add one or run 'apm marketplace init' to scaffold it."
        )
    if not isinstance(raw_block, dict):
        raise MarketplaceYmlError("'marketplace' in apm.yml must be a mapping")

    _check_unknown_keys(raw_block, _APM_MARKETPLACE_KEYS, context="marketplace")

    top_name = data.get("name")
    top_desc = data.get("description")
    top_ver = data.get("version")

    name_overridden = "name" in raw_block and raw_block["name"] is not None
    desc_overridden = "description" in raw_block and raw_block["description"] is not None
    ver_overridden = "version" in raw_block and raw_block["version"] is not None

    if name_overridden:
        name = _require_str(raw_block, "name", context="marketplace")
    else:
        if not isinstance(top_name, str) or not top_name.strip():
            raise MarketplaceYmlError(
                "'name' is required (set it at apm.yml top level or override via marketplace.name)"
            )
        name = top_name.strip()

    if desc_overridden:
        description = _require_str(raw_block, "description", context="marketplace")
    else:
        description = top_desc.strip() if isinstance(top_desc, str) and top_desc.strip() else ""

    if ver_overridden:
        version_str = _require_str(raw_block, "version", context="marketplace")
    else:
        version_str = str(top_ver).strip() if top_ver is not None else ""

    if version_str:
        _validate_semver(version_str, context="version")

    return _build_config(
        marketplace_dict=raw_block,
        name=name,
        description=description,
        version=version_str,
        source_path=apm_yml_path,
        is_legacy=False,
        name_overridden=name_overridden,
        description_overridden=desc_overridden,
        version_overridden=ver_overridden,
    )


# ---------------------------------------------------------------------------
# Shared internal config assembler
# ---------------------------------------------------------------------------


def _build_config(
    *,
    marketplace_dict: dict[str, Any],
    name: str,
    description: str,
    version: str,
    source_path: Path,
    is_legacy: bool,
    name_overridden: bool,
    description_overridden: bool,
    version_overridden: bool,
    default_output: str = ".claude-plugin/marketplace.json",
) -> MarketplaceConfig:
    """Assemble a MarketplaceConfig from an already-parsed dict.

    Delegates field-level parsing to ``_yml_parsers._build_config_fields``
    which owns the sub-block parsers.  This function owns only the
    top-level wiring: path-traversal guard on ``output``, sibling-vs-map
    conflict detection, and the duplicate-package-name check.
    """
    warnings_sink: list[str] = []

    (
        owner,
        outputs,
        output_specs,
        output,
        claude,
        metadata,
        build,
        versioning,
        codex,
    ) = _build_config_fields(marketplace_dict, default_output, warnings_sink)

    # S1: validate pluginRoot with path-safety checks if present.
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

    # -- marketplace source base --
    source_base = parse_source_base(marketplace_dict.get("sourceBase"))

    # -- Sibling-vs-map conflict detection (A1: sibling wins) --
    # Only fire when the user EXPLICITLY set a sibling block AND the map
    # also has an explicit path. Default/absent sibling is not a conflict.
    has_explicit_claude = marketplace_dict.get("claude") is not None
    has_explicit_codex = marketplace_dict.get("codex") is not None
    output_specs = _resolve_output_spec_conflicts(
        output_specs,
        claude,
        codex,
        has_explicit_claude,
        has_explicit_codex,
        warnings_sink,
    )

    # Packages
    raw_packages = marketplace_dict.get("packages") or []
    if not isinstance(raw_packages, list):
        raise MarketplaceYmlError("'packages' must be a list")

    entries: list[PackageEntry] = []
    seen_names: dict[str, int] = {}
    for idx, raw_entry in enumerate(raw_packages):
        entry = _parse_package_entry(raw_entry, idx, source_base=source_base)
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
            missing = [e.name for e in entries if not getattr(e, field_name)]
            if missing:
                raise MarketplaceYmlError(
                    f"packages must define '{field_name}' when marketplace.outputs includes "
                    f"'{output_name}' (missing: {', '.join(missing)})"
                )

    return MarketplaceConfig(
        name=name,
        description=description,
        version=version,
        owner=owner,
        output=output,
        outputs=outputs,
        claude=claude,
        codex=codex,
        metadata=metadata,
        build=build,
        source_base=source_base,
        versioning=versioning,
        packages=tuple(entries),
        output_specs=output_specs,
        warnings=tuple(warnings_sink),
        source_path=source_path,
        is_legacy=is_legacy,
        name_overridden=name_overridden,
        description_overridden=description_overridden,
        version_overridden=version_overridden,
    )


def _resolve_output_spec_conflicts(
    output_specs: tuple[MarketplaceOutputSpec, ...],
    claude: MarketplaceClaudeConfig,
    codex: MarketplaceCodexConfig,
    has_explicit_claude: bool,
    has_explicit_codex: bool,
    warnings_sink: list[str],
) -> tuple[MarketplaceOutputSpec, ...]:
    """Apply sibling-wins rule when outputs map and sibling block conflict."""
    final_specs_list = list(output_specs)
    for i, spec in enumerate(final_specs_list):
        if not spec.path_explicit:
            continue
        sibling_path: str | None = None
        if spec.name == "claude" and has_explicit_claude and claude.output != spec.path:
            sibling_path = claude.output
        elif spec.name == "codex" and has_explicit_codex and codex.output != spec.path:
            sibling_path = codex.output
        if sibling_path is None:
            continue
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
        final_specs_list[i] = MarketplaceOutputSpec(
            name=spec.name,
            path=sibling_path,
            path_explicit=True,
        )
    return tuple(final_specs_list)
