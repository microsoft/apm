"""Dataclasses, loader, and validation for ``marketplace.yml``.

``marketplace.yml`` is the maintainer-authored source file that the
``mkt-builder`` compiles into an Anthropic-compliant ``marketplace.json``.
This module is responsible for parsing the YAML, enforcing structural
constraints, and producing immutable dataclass instances that downstream
code can inspect without further validation.

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
* **Strict top-level and per-entry key sets.**  Unknown keys raise
  ``MarketplaceYmlError`` immediately so that typos are never silently
  ignored.
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
    "MarketplaceYml",
    "MarketplaceOwner",
    "MarketplaceBuild",
    "PackageEntry",
    "MarketplaceYmlError",
    "SOURCE_RE",
    "load_marketplace_yml",
]

# ---------------------------------------------------------------------------
# Semver validation (matches codebase convention -- regex, no external lib)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# ``owner/repo`` shape -- at least one char on each side of the slash.
# Used by both yml_schema and yml_editor for source field validation.
SOURCE_RE = re.compile(r"^[^/]+/[^/]+$")

# Placeholder tokens accepted in ``tag_pattern`` / ``build.tagPattern``.
_TAG_PLACEHOLDERS = ("{version}", "{name}")

# ---------------------------------------------------------------------------
# Permitted key sets (strict mode)
# ---------------------------------------------------------------------------

_TOP_LEVEL_KEYS = frozenset({
    "name",
    "description",
    "version",
    "owner",
    "output",
    "metadata",
    "build",
    "packages",
})

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
    "tags",
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
    ``tags``) are stored alongside APM-only attributes (``subdir``,
    ``version``, ``ref``, ``tag_pattern``, ``include_prerelease``) so
    the builder can partition them at compile time.
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
    tags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketplaceYml:
    """Top-level representation of a parsed ``marketplace.yml``.

    ``metadata`` is stored as a plain ``dict`` preserving the original
    key casing so the builder can forward it verbatim to
    ``marketplace.json``.
    """

    name: str
    description: str
    version: str
    owner: MarketplaceOwner
    output: str = "marketplace.json"
    metadata: Dict[str, Any] = field(default_factory=dict)
    build: MarketplaceBuild = field(default_factory=MarketplaceBuild)
    packages: Tuple[PackageEntry, ...] = ()


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
    """Validate ``source`` field shape and path safety."""
    ctx = f"packages[{index}].source"
    if not SOURCE_RE.match(source):
        raise MarketplaceYmlError(
            f"'{ctx}' must match '<owner>/<repo>' shape, got '{source}'"
        )
    try:
        validate_path_segments(source, context=ctx)
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

    # APM-only: subdir
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

    # At least one of version or ref must be present
    if version is None and ref is None:
        raise MarketplaceYmlError(
            f"packages[{index}] ('{name}'): at least one of "
            f"'version' or 'ref' must be set"
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
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_marketplace_yml(path: Path) -> MarketplaceYml:
    """Load and validate a ``marketplace.yml`` file.

    Parameters
    ----------
    path : Path
        Filesystem path to the YAML file.

    Returns
    -------
    MarketplaceYml
        Fully validated, immutable representation.

    Raises
    ------
    MarketplaceYmlError
        On any validation failure or YAML parse error.
    """
    # -- read + parse YAML --
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketplaceYmlError(
            f"Cannot read '{path}': {exc}"
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Include line number when the YAML library provides it.
        detail = ""
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            mark = exc.problem_mark
            detail = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise MarketplaceYmlError(
            f"YAML parse error in '{path}'{detail}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise MarketplaceYmlError(
            f"'{path}' must contain a YAML mapping at the top level"
        )

    # -- strict top-level key check --
    _check_unknown_keys(data, _TOP_LEVEL_KEYS, context="top level")

    # -- required scalars --
    name = _require_str(data, "name")
    description = _require_str(data, "description")
    version_str = _require_str(data, "version")
    _validate_semver(version_str, context="version")

    # -- owner --
    raw_owner = data.get("owner")
    if raw_owner is None:
        raise MarketplaceYmlError("'owner' is required")
    owner = _parse_owner(raw_owner)

    # -- output --
    output = data.get("output", "marketplace.json")
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError(
            "'output' must be a non-empty string"
        )
    output = output.strip()

    # Path-traversal guard -- reject output paths containing ".." segments.
    try:
        validate_path_segments(output, context="marketplace.yml output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    # -- metadata (Anthropic pass-through, preserve verbatim) --
    metadata: Dict[str, Any] = {}
    raw_metadata = data.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict):
            raise MarketplaceYmlError("'metadata' must be a mapping")
        metadata = dict(raw_metadata)

    # -- build --
    build = _parse_build(data.get("build"))

    # -- packages --
    raw_packages = data.get("packages")
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

    return MarketplaceYml(
        name=name,
        description=description,
        version=version_str,
        owner=owner,
        output=output,
        metadata=metadata,
        build=build,
        packages=tuple(entries),
    )
