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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..utils.path_security import PathTraversalError, validate_path_segments
from .errors import MarketplaceYmlError

__all__ = [
    "MarketplaceConfig",
    "MarketplaceYml",  # backwards-compat alias
    "MarketplaceOwner",
    "MarketplaceBuild",
    "PackageEntry",
    "MarketplaceYmlError",
    "SOURCE_RE",
    "LOCAL_SOURCE_RE",
    "load_marketplace_yml",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
]

# ---------------------------------------------------------------------------
# Semver validation (matches codebase convention -- regex, no external lib)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# Source field accepts either ``owner/repo`` (remote) or ``./...`` (local
# path within the same repo).  Used by both yml_schema and yml_editor for
# source field validation.
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")

# Placeholder tokens accepted in ``tag_pattern`` / ``build.tagPattern``.
_TAG_PLACEHOLDERS = ("{version}", "{name}")

# ---------------------------------------------------------------------------
# Permitted key sets (strict mode)
# ---------------------------------------------------------------------------

_BUILD_KEYS = frozenset({
    "tagPattern",
})

_PACKAGE_ENTRY_KEYS = frozenset({
    "name",
    "source",
    "subdir",
    "version",
    "ref",
    "tag_pattern",
    "include_prerelease",
    "description",
    "homepage",
    "tags",
})

# Keys permitted inside the ``marketplace:`` block of apm.yml.  This is
# distinct from the legacy top-level keys (which include ``name``,
# ``description``, ``version`` -- those are inherited from apm.yml's
# top-level scalars in the new world).
_APM_MARKETPLACE_KEYS = frozenset({
    "name",          # optional override of top-level apm.yml name
    "description",   # optional override of top-level apm.yml description
    "version",       # optional override of top-level apm.yml version
    "owner",
    "output",
    "metadata",
    "build",
    "packages",
})
# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketplaceOwner:
    """Owner block of ``marketplace.yml``."""

    name: str
    email: Optional[str] = None
    url: Optional[str] = None


@dataclass(frozen=True)
class MarketplaceBuild:
    """APM-only build configuration block."""

    tag_pattern: str = "v{version}"


@dataclass(frozen=True)
class PackageEntry:
    """A single entry in the ``packages`` list.

    Attributes that are Anthropic pass-through (``description``,
    ``homepage``, ``tags``) are stored alongside APM-only attributes
    (``subdir``, ``version``, ``ref``, ``tag_pattern``,
    ``include_prerelease``) so the builder can partition them at
    compile time.

    ``is_local`` is derived by the loader from the ``source`` field --
    a leading ``./`` marks a local-path package that skips git
    resolution.
    """

    name: str
    source: str
    # APM-only fields
    subdir: Optional[str] = None
    version: Optional[str] = None
    ref: Optional[str] = None
    tag_pattern: Optional[str] = None
    include_prerelease: bool = False
    # Anthropic pass-through fields
    description: Optional[str] = None
    homepage: Optional[str] = None
    tags: Tuple[str, ...] = ()
    # Derived (set by loader, not by user)
    is_local: bool = False


@dataclass(frozen=True)
class MarketplaceConfig:
    """Parsed marketplace configuration.

    May originate from apm.yml's ``marketplace:`` block (current) or
    from a standalone ``marketplace.yml`` (legacy, deprecated).

    ``metadata`` is stored as a plain ``dict`` preserving the original
    key casing so the builder can forward it verbatim to
    ``marketplace.json``.

    Override flags (``*_overridden``) record whether the marketplace
    block explicitly set each inheritable field.  The builder uses
    these flags to decide whether to emit ``description``/``version``
    at the top level of ``marketplace.json`` -- per the Anthropic
    azure-skills convention, inherited values are omitted from output.
    """

    name: str
    description: str
    version: str
    owner: MarketplaceOwner
    output: str = ".claude-plugin/marketplace.json"
    metadata: Dict[str, Any] = field(default_factory=dict)
    build: MarketplaceBuild = field(default_factory=MarketplaceBuild)
    packages: Tuple[PackageEntry, ...] = ()
    # Origin tracking + override-detection metadata
    source_path: Optional[Path] = None
    is_legacy: bool = False
    name_overridden: bool = False
    description_overridden: bool = False
    version_overridden: bool = False


# Backwards-compatibility alias for callers that still import
# ``MarketplaceYml``.  Will be removed in a future minor release.
MarketplaceYml = MarketplaceConfig


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_str(
    data: Dict[str, Any],
    key: str,
    *,
    context: str = "",
) -> str:
    """Return a non-empty string value or raise ``MarketplaceYmlError``."""
    path = f"{context}.{key}" if context else key
    value = data.get(key)
    if value is None:
        raise MarketplaceYmlError(f"'{path}' is required")
    if not isinstance(value, str) or not value.strip():
        raise MarketplaceYmlError(
            f"'{path}' must be a non-empty string"
        )
    return value.strip()


def _validate_semver(version: str, *, context: str = "version") -> None:
    """Raise if *version* is not a valid semver string."""
    if not _SEMVER_RE.match(version):
        raise MarketplaceYmlError(
            f"'{context}' value '{version}' is not valid semver (expected x.y.z)"
        )


def _validate_source(source: str, *, index: int) -> None:
    """Validate ``source`` field shape and path safety.

    Accepts either ``owner/repo`` (remote) or ``./...`` (local path).
    """
    ctx = f"packages[{index}].source"
    if not SOURCE_RE.match(source):
        raise MarketplaceYmlError(
            f"'{ctx}' must match '<owner>/<repo>' or './<path>' shape, "
            f"got '{source}'"
        )
    is_local = bool(LOCAL_SOURCE_RE.match(source))
    try:
        # Local paths legitimately start with ``.`` (current dir) and
        # may have trailing-slash forms like ``./``.  Allow ``.`` here.
        validate_path_segments(
            source, context=ctx, allow_current_dir=is_local
        )
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc


def _validate_tag_pattern(pattern: str, *, context: str) -> None:
    """Ensure *pattern* contains at least one recognised placeholder."""
    if not any(ph in pattern for ph in _TAG_PLACEHOLDERS):
        raise MarketplaceYmlError(
            f"'{context}' must contain at least one of "
            f"{', '.join(_TAG_PLACEHOLDERS)}, got '{pattern}'"
        )


def _check_unknown_keys(
    data: Dict[str, Any],
    permitted: frozenset,
    *,
    context: str,
) -> None:
    """Raise on any key not in *permitted*."""
    unknown = set(data.keys()) - permitted
    if unknown:
        sorted_unknown = sorted(unknown)
        sorted_permitted = sorted(permitted)
        raise MarketplaceYmlError(
            f"Unknown key(s) in {context}: {', '.join(sorted_unknown)}. "
            f"Permitted keys: {', '.join(sorted_permitted)}"
        )


# ---------------------------------------------------------------------------
# Internal parse helpers
# ---------------------------------------------------------------------------


def _parse_owner(raw: Any) -> MarketplaceOwner:
    """Parse and validate the ``owner`` block."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(
            "'owner' must be a mapping with at least a 'name' key"
        )
    name = _require_str(raw, "name", context="owner")
    email = raw.get("email")
    if email is not None:
        email = str(email).strip() or None
    url = raw.get("url")
    if url is not None:
        url = str(url).strip() or None
    return MarketplaceOwner(name=name, email=email, url=url)


def _parse_build(raw: Any) -> MarketplaceBuild:
    """Parse and validate the ``build`` block."""
    if raw is None:
        return MarketplaceBuild()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'build' must be a mapping")
    _check_unknown_keys(raw, _BUILD_KEYS, context="build")
    tag_pattern = raw.get("tagPattern", "v{version}")
    if not isinstance(tag_pattern, str) or not tag_pattern.strip():
        raise MarketplaceYmlError(
            "'build.tagPattern' must be a non-empty string"
        )
    tag_pattern = tag_pattern.strip()
    _validate_tag_pattern(tag_pattern, context="build.tagPattern")
    return MarketplaceBuild(tag_pattern=tag_pattern)


def _parse_package_entry(raw: Any, index: int) -> PackageEntry:
    """Parse and validate a single ``packages`` entry."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(
            f"packages[{index}] must be a mapping"
        )

    # -- strict key check --
    _check_unknown_keys(raw, _PACKAGE_ENTRY_KEYS, context=f"packages[{index}]")

    name = _require_str(raw, "name", context=f"packages[{index}]")
    source = _require_str(raw, "source", context=f"packages[{index}]")
    _validate_source(source, index=index)
    is_local = bool(LOCAL_SOURCE_RE.match(source))

    # APM-only: subdir (irrelevant for local packages but harmless)
    subdir: Optional[str] = raw.get("subdir")
    if subdir is not None:
        if not isinstance(subdir, str) or not subdir.strip():
            raise MarketplaceYmlError(
                f"'packages[{index}].subdir' must be a non-empty string"
            )
        subdir = subdir.strip()
        try:
            validate_path_segments(subdir, context=f"packages[{index}].subdir")
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc

    # APM-only: version (semver range -- stored as string, not parsed here)
    version: Optional[str] = raw.get("version")
    if version is not None:
        version = str(version).strip()
        if not version:
            raise MarketplaceYmlError(
                f"'packages[{index}].version' must be a non-empty string"
            )

    # APM-only: ref
    ref: Optional[str] = raw.get("ref")
    if ref is not None:
        ref = str(ref).strip()
        if not ref:
            raise MarketplaceYmlError(
                f"'packages[{index}].ref' must be a non-empty string"
            )

    # At least one of version or ref must be present for REMOTE packages.
    # Local-path packages skip git resolution so the requirement does not
    # apply to them.
    if not is_local and version is None and ref is None:
        raise MarketplaceYmlError(
            f"packages[{index}] ('{name}'): remote packages require at "
            f"least one of 'version' or 'ref'"
        )

    # APM-only: tag_pattern
    tag_pattern: Optional[str] = raw.get("tag_pattern")
    if tag_pattern is not None:
        if not isinstance(tag_pattern, str) or not tag_pattern.strip():
            raise MarketplaceYmlError(
                f"'packages[{index}].tag_pattern' must be a non-empty string"
            )
        tag_pattern = tag_pattern.strip()
        _validate_tag_pattern(
            tag_pattern, context=f"packages[{index}].tag_pattern"
        )

    # APM-only: include_prerelease
    include_prerelease = raw.get("include_prerelease", False)
    if not isinstance(include_prerelease, bool):
        raise MarketplaceYmlError(
            f"'packages[{index}].include_prerelease' must be a boolean"
        )

    # Anthropic pass-through: description
    description: Optional[str] = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            raise MarketplaceYmlError(
                f"'packages[{index}].description' must be a non-empty string"
            )
        description = description.strip()

    # Anthropic pass-through: homepage
    homepage: Optional[str] = raw.get("homepage")
    if homepage is not None:
        if not isinstance(homepage, str) or not homepage.strip():
            raise MarketplaceYmlError(
                f"'packages[{index}].homepage' must be a non-empty string"
            )
        homepage = homepage.strip()

    # Anthropic pass-through: tags
    raw_tags = raw.get("tags")
    tags: Tuple[str, ...] = ()
    if raw_tags is not None:
        if not isinstance(raw_tags, list):
            raise MarketplaceYmlError(
                f"'packages[{index}].tags' must be a list of strings"
            )
        tags = tuple(str(t) for t in raw_tags)

    return PackageEntry(
        name=name,
        source=source,
        subdir=subdir,
        version=version,
        ref=ref,
        tag_pattern=tag_pattern,
        include_prerelease=include_prerelease,
        description=description,
        homepage=homepage,
        tags=tags,
        is_local=is_local,
    )


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

    # -- strict top-level key check --
    _check_unknown_keys(data, _APM_MARKETPLACE_KEYS, context="top level")

    # -- required scalars --
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
        raise MarketplaceYmlError(
            "'marketplace' in apm.yml must be a mapping"
        )

    # -- strict marketplace-block key check --
    _check_unknown_keys(
        raw_block, _APM_MARKETPLACE_KEYS, context="marketplace"
    )

    # -- inheritance with optional overrides --
    top_name = data.get("name")
    top_desc = data.get("description")
    top_ver = data.get("version")

    name_overridden = "name" in raw_block and raw_block["name"] is not None
    desc_overridden = (
        "description" in raw_block and raw_block["description"] is not None
    )
    ver_overridden = (
        "version" in raw_block and raw_block["version"] is not None
    )

    if name_overridden:
        name = _require_str(raw_block, "name", context="marketplace")
    else:
        if not isinstance(top_name, str) or not top_name.strip():
            raise MarketplaceYmlError(
                "'name' is required (set it at apm.yml top level or "
                "override via marketplace.name)"
            )
        name = top_name.strip()

    if desc_overridden:
        description = _require_str(
            raw_block, "description", context="marketplace"
        )
    else:
        if not isinstance(top_desc, str) or not top_desc.strip():
            description = ""
        else:
            description = top_desc.strip()

    if ver_overridden:
        version_str = _require_str(
            raw_block, "version", context="marketplace"
        )
    else:
        if top_ver is None:
            version_str = ""
        else:
            version_str = str(top_ver).strip()

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
# Shared internal helpers
# ---------------------------------------------------------------------------


def _read_yaml_mapping(path: Path) -> Dict[str, Any]:
    """Read *path* and return its top-level mapping or raise."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketplaceYmlError(
            f"Cannot read '{path}': {exc}"
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = ""
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            mark = exc.problem_mark
            detail = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise MarketplaceYmlError(
            f"YAML parse error in '{path}'{detail}: {exc}"
        ) from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MarketplaceYmlError(
            f"'{path}' must contain a YAML mapping at the top level"
        )
    return data


def _build_config(
    *,
    marketplace_dict: Dict[str, Any],
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
    """Shared parser for the marketplace fields once name/desc/version
    have been resolved (either inherited or read directly).
    """
    # -- owner --
    raw_owner = marketplace_dict.get("owner")
    if raw_owner is None:
        raise MarketplaceYmlError("'owner' is required")
    owner = _parse_owner(raw_owner)

    # -- output (default differs between legacy and new layouts) --
    output = marketplace_dict.get("output")
    if output is None:
        output = default_output
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError(
            "'output' must be a non-empty string"
        )
    output = output.strip()

    # Path-traversal guard -- reject output paths containing ".." segments.
    try:
        validate_path_segments(output, context="marketplace output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    # -- metadata (Anthropic pass-through, preserve verbatim) --
    metadata: Dict[str, Any] = {}
    raw_metadata = marketplace_dict.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict):
            raise MarketplaceYmlError("'metadata' must be a mapping")
        metadata = dict(raw_metadata)

    # -- build --
    build = _parse_build(marketplace_dict.get("build"))

    # -- packages --
    raw_packages = marketplace_dict.get("packages")
    if raw_packages is None:
        raw_packages = []
    if not isinstance(raw_packages, list):
        raise MarketplaceYmlError("'packages' must be a list")

    entries: List[PackageEntry] = []
    seen_names: Dict[str, int] = {}
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

    return MarketplaceConfig(
        name=name,
        description=description,
        version=version,
        owner=owner,
        output=output,
        metadata=metadata,
        build=build,
        packages=tuple(entries),
        source_path=source_path,
        is_legacy=is_legacy,
        name_overridden=name_overridden,
        description_overridden=description_overridden,
        version_overridden=version_overridden,
    )
