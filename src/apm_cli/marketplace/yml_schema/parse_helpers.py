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
from typing import Any

from ...utils.path_security import PathTraversalError, validate_path_segments
from ..errors import MarketplaceYmlError
from ..output_profiles import MARKETPLACE_OUTPUTS, known_output_names
from ._package_field_helpers import (
    _merge_and_cap_tags,
    _optional_non_empty_string,
    _optional_validated_path,
    _parse_author,
    _parse_package_tags,
)
from .class_ import (
    MarketplaceBuild,
    MarketplaceClaudeConfig,
    MarketplaceCodexConfig,
    MarketplaceConfig,
    MarketplaceOutputSpec,
    MarketplaceOwner,
    MarketplaceVersioning,
    PackageEntry,
)

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")
_TAG_PLACEHOLDERS = ("{version}", "{name}")
_BUILD_KEYS = frozenset(
    {
        "tagPattern",
    }
)
_PACKAGE_ENTRY_KEYS = frozenset(
    {
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
        "author",
        "license",
        "repository",
        "keywords",
        "category",
    }
)
_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",  # optional override of top-level apm.yml name
        "description",  # optional override of top-level apm.yml description
        "version",  # optional override of top-level apm.yml version
        "owner",
        "output",
        "outputs",
        "claude",
        "metadata",
        "build",
        "codex",
        "packages",
    }
)
_CLAUDE_KEYS = frozenset(
    {
        "output",
    }
)
_CODEX_KEYS = frozenset(
    {
        "output",
    }
)
_VERSIONING_KEYS = frozenset({"strategy"})
_VERSIONING_STRATEGIES = frozenset({"lockstep", "tag_pattern", "per_package"})
MarketplaceYml = MarketplaceConfig


def _require_str(
    data: dict[str, Any],
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
        raise MarketplaceYmlError(f"'{path}' must be a non-empty string")
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
            f"'{ctx}' must match '<owner>/<repo>' or './<path>' shape, got '{source}'"
        )
    is_local = bool(LOCAL_SOURCE_RE.match(source))
    try:
        # Local paths legitimately start with ``.`` (current dir) and
        # may have trailing-slash forms like ``./``.  Allow ``.`` here.
        validate_path_segments(source, context=ctx, allow_current_dir=is_local)
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
    data: dict[str, Any],
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


def _parse_owner(raw: Any) -> MarketplaceOwner:
    """Parse and validate the ``owner`` block."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'owner' must be a mapping with at least a 'name' key")
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
        raise MarketplaceYmlError("'build.tagPattern' must be a non-empty string")
    tag_pattern = tag_pattern.strip()
    _validate_tag_pattern(tag_pattern, context="build.tagPattern")
    return MarketplaceBuild(tag_pattern=tag_pattern)


def _parse_versioning(raw: Any) -> MarketplaceVersioning:
    """Parse and validate the optional ``marketplace.versioning`` block."""
    if raw is None:
        return MarketplaceVersioning()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"'versioning' must be a mapping, got {type(raw).__name__}")
    _check_unknown_keys(raw, _VERSIONING_KEYS, context="versioning")
    strategy = raw.get("strategy", "lockstep")
    if not isinstance(strategy, str) or not strategy.strip():
        raise MarketplaceYmlError("'versioning.strategy' must be a non-empty string")
    strategy = strategy.strip()
    if strategy not in _VERSIONING_STRATEGIES:
        valid = ", ".join(sorted(_VERSIONING_STRATEGIES))
        raise MarketplaceYmlError(
            f"'versioning.strategy' must be one of: {valid}; got {strategy!r}"
        )
    return MarketplaceVersioning(strategy=strategy)


def _parse_claude(raw: Any, *, default_output: str) -> MarketplaceClaudeConfig:
    """Parse and validate the optional ``marketplace.claude`` block."""
    if raw is None:
        return MarketplaceClaudeConfig(output=default_output)
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'claude' must be a mapping")
    _check_unknown_keys(raw, _CLAUDE_KEYS, context="claude")

    output = raw.get("output", default_output)
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'claude.output' must be a non-empty string")
    output = output.strip()
    try:
        validate_path_segments(output, context="claude.output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    return MarketplaceClaudeConfig(output=output)


def _parse_codex(raw: Any) -> MarketplaceCodexConfig:
    """Parse and validate the optional ``marketplace.codex`` block."""
    if raw is None:
        return MarketplaceCodexConfig()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'codex' must be a mapping")
    _check_unknown_keys(raw, _CODEX_KEYS, context="codex")

    output = raw.get("output", MARKETPLACE_OUTPUTS["codex"].default_output)
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'codex.output' must be a non-empty string")
    output = output.strip()
    try:
        validate_path_segments(output, context="codex.output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    return MarketplaceCodexConfig(output=output)


from ._parse_outputs import (
    _append_outputs_deprecation_warning,
    _parse_output_map_entry,
    _parse_outputs,
    _parse_outputs_map,
    _validate_output_name,
)


def _parse_package_entry(raw: Any, index: int) -> PackageEntry:
    """Parse and validate a single ``packages`` entry."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"packages[{index}] must be a mapping")

    _check_unknown_keys(raw, _PACKAGE_ENTRY_KEYS, context=f"packages[{index}]")
    name = _require_str(raw, "name", context=f"packages[{index}]")
    source = _require_str(raw, "source", context=f"packages[{index}]")
    _validate_source(source, index=index)
    is_local = bool(LOCAL_SOURCE_RE.match(source))

    subdir = _optional_validated_path(raw, "subdir", index=index)
    version = _optional_non_empty_string(raw, "version", index=index)
    ref = _optional_non_empty_string(raw, "ref", index=index)
    if not is_local and version is None and ref is None:
        raise MarketplaceYmlError(
            f"packages[{index}] ('{name}'): remote packages require at least one of 'version' or 'ref'"
        )

    tag_pattern = _optional_non_empty_string(raw, "tag_pattern", index=index)
    if tag_pattern is not None:
        _validate_tag_pattern(tag_pattern, context=f"packages[{index}].tag_pattern")

    include_prerelease = raw.get("include_prerelease", False)
    if not isinstance(include_prerelease, bool):
        raise MarketplaceYmlError(f"'packages[{index}].include_prerelease' must be a boolean")

    description = _optional_non_empty_string(raw, "description", index=index)
    homepage = _optional_non_empty_string(raw, "homepage", index=index)
    tags = _parse_package_tags(raw, "tags", index=index)
    keywords = _parse_package_tags(raw, "keywords", index=index)
    tags = _merge_and_cap_tags(tags=tags, keywords=keywords, index=index, name=name)
    author = _parse_author(raw.get("author"), index)
    license_val = _optional_non_empty_string(raw, "license", index=index)
    repository = _optional_non_empty_string(raw, "repository", index=index)
    category = _optional_non_empty_string(raw, "category", index=index)

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
        author=author,
        license=license_val,
        repository=repository,
        category=category,
        is_local=is_local,
    )
