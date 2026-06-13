"""Parse helpers and validation functions for marketplace YAML configs.

Leaf module -- imports from ``._yml_models`` but never from
``yml_schema`` (cycle-safe).  All public symbols used by ``yml_schema``
are imported from here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ..utils.path_security import PathTraversalError, validate_path_segments
from ._yml_models import (
    MarketplaceBuild,
    MarketplaceClaudeConfig,
    MarketplaceCodexConfig,
    MarketplaceOutputSpec,
    MarketplaceOwner,
    MarketplaceVersioning,
    PackageEntry,
)
from ._yml_source import (
    SOURCE_BASE_RE as SOURCE_BASE_RE,
)
from ._yml_source import (
    parse_source_base as parse_source_base,
)
from ._yml_source import (
    split_source_base as split_source_base,
)
from ._yml_source import (
    validate_source_value as validate_source_value,
)
from .errors import MarketplaceYmlError
from .output_profiles import MARKETPLACE_OUTPUTS, known_output_names

__all__ = [
    "LOCAL_SOURCE_RE",
    "SOURCE_BASE_RE",
    "SOURCE_RE",
    "parse_source_base",
    "split_host_from_source",
    "split_source_base",
    "validate_source_value",
]

# ---------------------------------------------------------------------------
# Semver validation (regex, no external lib)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# ---------------------------------------------------------------------------
# Source field patterns
# ---------------------------------------------------------------------------

# Source field accepts:
#   - ``owner/repo`` (remote, default host)
#   - ``host.tld/owner/repo`` (remote on a non-default host, shorthand)
#   - ``https://host.tld/owner/repo`` (remote on a non-default host, full URL)
#   - ``https://host.tld/owner/repo.git`` (same, with optional ``.git`` suffix)
#   - ``./...`` (local path within the same repo)
_HOST_PAT = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]*"
# SECURITY: segment regexes are shape filters only. Traversal defence lives in
# validate_path_segments(), which rejects empty, '.', and '..' path segments.
_SEGMENT_PAT = r"[A-Za-z0-9._-]+"
_OWNER_REPO_PAT = rf"{_SEGMENT_PAT}/{_SEGMENT_PAT}"

SOURCE_RE = re.compile(
    r"^(?:"
    rf"https://{_HOST_PAT}/{_OWNER_REPO_PAT}(?:\.git)?"
    rf"|{_HOST_PAT}/{_OWNER_REPO_PAT}"
    rf"|{_OWNER_REPO_PAT}"
    r"|\./.*"
    r")$"
)
LOCAL_SOURCE_RE = re.compile(r"^\./")
# Matches ``host.tld/owner/repo`` (3 segments, first is FQDN-ish).
_HOST_PREFIXED_SOURCE_RE = re.compile(rf"^({_HOST_PAT})/({_OWNER_REPO_PAT})$")
# Matches ``https://host.tld/owner/repo[.git]`` and captures host + owner/repo.
_HTTPS_URL_SOURCE_RE = re.compile(rf"^https://({_HOST_PAT})/({_OWNER_REPO_PAT})(?:\.git)?$")

# ---------------------------------------------------------------------------
# Tag-pattern placeholders
# ---------------------------------------------------------------------------

_TAG_PLACEHOLDERS = ("{version}", "{name}")

# ---------------------------------------------------------------------------
# Permitted key sets (strict mode)
# ---------------------------------------------------------------------------


_BUILD_KEYS = frozenset({"tagPattern"})

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

# Limits for keywords/tags array to prevent DoS via oversized manifests (S4).
_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100

# Keys permitted inside an ``author`` object.
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})

_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",
        "description",
        "version",
        "owner",
        "sourceBase",
        "output",
        "outputs",
        "claude",
        "metadata",
        "build",
        "codex",
        "packages",
        "versioning",
    }
)

_VERSIONING_KEYS = frozenset({"strategy"})
_VERSIONING_STRATEGIES = frozenset({"lockstep", "tag_pattern", "per_package"})
_CLAUDE_KEYS = frozenset({"output"})
_CODEX_KEYS = frozenset({"output"})

# ---------------------------------------------------------------------------
# Public: source field splitters
# ---------------------------------------------------------------------------


def split_host_from_source(source: str) -> tuple[str | None, str]:
    """Split a host-qualified source into ``(host, owner/repo)``.

    Accepts both shorthand (``host.tld/owner/repo``) and full HTTPS URL
    (``https://host.tld/owner/repo[.git]``) forms.  Returns ``(None, source)``
    for the plain ``owner/repo`` shorthand or local ``./...`` paths.
    """
    m = _HTTPS_URL_SOURCE_RE.match(source)
    if m:
        host, owner_repo = m.group(1), m.group(2)
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[: -len(".git")]
        return host, owner_repo
    m = _HOST_PREFIXED_SOURCE_RE.match(source)
    if m:
        return m.group(1), m.group(2)
    return None, source


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_str(data: dict[str, Any], key: str, *, context: str = "") -> str:
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


def _validate_source(source: str, *, index: int, source_base: str | None = None) -> None:
    """Validate ``source`` field shape and path safety."""
    validate_source_value(
        source,
        context=f"packages[{index}].source",
        source_base=source_base,
    )


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
        raise MarketplaceYmlError(
            f"Unknown key(s) in {context}: {', '.join(sorted(unknown))}. "
            f"Permitted keys: {', '.join(sorted(permitted))}"
        )


# ---------------------------------------------------------------------------
# Internal parse helpers
# ---------------------------------------------------------------------------


def _parse_author(raw: Any, index: int) -> dict[str, str] | None:
    """Normalize a curator-supplied ``author`` value.

    Accepts either a non-empty string (treated as ``name``) or a mapping
    with at least ``name`` and only the permitted keys.
    Returns ``None`` when ``raw`` is ``None``.
    """
    if raw is None:
        return None
    ctx = f"packages[{index}].author"
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            raise MarketplaceYmlError(f"'{ctx}' must be a non-empty string or object with 'name'")
        return {"name": name}
    if isinstance(raw, dict):
        unknown = set(raw.keys()) - _AUTHOR_OBJECT_KEYS
        if unknown:
            raise MarketplaceYmlError(
                f"'{ctx}' has unknown key(s): "
                f"{', '.join(sorted(unknown))}; allowed: "
                f"{', '.join(sorted(_AUTHOR_OBJECT_KEYS))}"
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MarketplaceYmlError(f"'{ctx}.name' is required and must be a non-empty string")
        out: dict[str, str] = {"name": name.strip()}
        for key in ("email", "url"):
            val = raw.get(key)
            if val is None:
                continue
            if not isinstance(val, str) or not val.strip():
                raise MarketplaceYmlError(f"'{ctx}.{key}' must be a non-empty string")
            out[key] = val.strip()
        return out
    raise MarketplaceYmlError(f"'{ctx}' must be a string or object, got {type(raw).__name__}")


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


def _parse_outputs(
    raw: Any,
    warnings_sink: list[str] | None = None,
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    """Parse the marketplace output selector.

    Accepts:
    - ``None`` -> default (claude only).
    - A list of strings -> back-compat list form (emits deprecation warning).
    - A string -> single-element back-compat list form.
    - A dict -> new map form with optional per-format ``path:``.

    Returns ``(outputs_tuple, output_specs_tuple)``.
    """
    if raw is None:
        default_spec = MarketplaceOutputSpec(
            name="claude",
            path=MARKETPLACE_OUTPUTS["claude"].default_output,
            path_explicit=False,
        )
        return ("claude",), (default_spec,)

    # --- Map form (new) ---
    if isinstance(raw, dict):
        return _parse_outputs_map(raw)

    # --- List / string form (deprecated back-compat) ---
    if isinstance(raw, str):
        raw_items: list[Any] = [raw]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise MarketplaceYmlError("'outputs' must be a string, list, or mapping")

    outputs_list: list[str] = []
    specs_list: list[MarketplaceOutputSpec] = []
    seen_set: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, str) or not item.strip():
            raise MarketplaceYmlError(f"'outputs[{index}]' must be a non-empty string")
        output = item.strip()
        known_outputs = known_output_names()
        if output not in known_outputs:
            raise MarketplaceYmlError(
                f"Unknown marketplace output '{output}'. "
                f"Permitted outputs: {', '.join(sorted(known_outputs))}"
            )
        if output in seen_set:
            raise MarketplaceYmlError(f"Duplicate marketplace output '{output}'")
        seen_set.add(output)
        outputs_list.append(output)
        specs_list.append(
            MarketplaceOutputSpec(
                name=output,
                path=MARKETPLACE_OUTPUTS[output].default_output,
                path_explicit=False,
            )
        )

    if not outputs_list:
        raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")

    names_str = ", ".join(outputs_list)
    map_lines = "\n".join(f"        {n}: {{}}" for n in outputs_list)
    deprecation_msg = (
        f"outputs: [{names_str}] is deprecated; use the map form:\n\n"
        f"      outputs:\n{map_lines}\n\n"
        f"    The list form will be removed in v0.15."
    )
    if warnings_sink is not None:
        warnings_sink.append(deprecation_msg)

    return tuple(outputs_list), tuple(specs_list)


def _parse_outputs_map(
    raw: dict[Any, Any],
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    """Parse the map form of the ``outputs:`` block."""
    outputs: list[str] = []
    specs: list[MarketplaceOutputSpec] = []
    seen: set[str] = set()
    known = known_output_names()

    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise MarketplaceYmlError("'outputs' map keys must be non-empty strings")
        name = key.strip()
        if name not in known:
            raise MarketplaceYmlError(
                f"Unknown marketplace output '{name}'. "
                f"Permitted outputs: {', '.join(sorted(known))}"
            )
        if name in seen:
            raise MarketplaceYmlError(f"Duplicate marketplace output '{name}'")
        seen.add(name)

        path_explicit = False
        path = MARKETPLACE_OUTPUTS[name].default_output
        if value is not None:
            if not isinstance(value, dict):
                raise MarketplaceYmlError(f"'outputs.{name}' must be a mapping or null")
            raw_path = value.get("path")
            if raw_path is not None:
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise MarketplaceYmlError(f"'outputs.{name}.path' must be a non-empty string")
                path = raw_path.strip()
                path_explicit = True
                try:
                    validate_path_segments(path, context=f"outputs.{name}.path")
                except PathTraversalError as exc:
                    raise MarketplaceYmlError(str(exc)) from exc
            unknown = set(value.keys()) - {"path"}
            if unknown:
                raise MarketplaceYmlError(
                    f"Unknown key(s) in 'outputs.{name}': {', '.join(sorted(unknown))}"
                )

        outputs.append(name)
        specs.append(MarketplaceOutputSpec(name=name, path=path, path_explicit=path_explicit))

    if not outputs:
        raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")
    return tuple(outputs), tuple(specs)


def _parse_package_entry(raw: Any, index: int, source_base: str | None = None) -> PackageEntry:
    """Parse and validate a single ``packages`` entry."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"packages[{index}] must be a mapping")

    _check_unknown_keys(raw, _PACKAGE_ENTRY_KEYS, context=f"packages[{index}]")

    name = _require_str(raw, "name", context=f"packages[{index}]")
    source = _require_str(raw, "source", context=f"packages[{index}]")
    _validate_source(source, index=index, source_base=source_base)
    is_local = bool(LOCAL_SOURCE_RE.match(source))
    host: str | None = None
    if not is_local:
        host, source = split_host_from_source(source)

    # APM-only: subdir
    subdir: str | None = raw.get("subdir")
    if subdir is not None:
        if not isinstance(subdir, str) or not subdir.strip():
            raise MarketplaceYmlError(f"'packages[{index}].subdir' must be a non-empty string")
        subdir = subdir.strip()
        try:
            validate_path_segments(subdir, context=f"packages[{index}].subdir")
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc

    # APM-only: version
    version: str | None = raw.get("version")
    if version is not None:
        version = str(version).strip()
        if not version:
            raise MarketplaceYmlError(f"'packages[{index}].version' must be a non-empty string")

    # APM-only: ref
    ref: str | None = raw.get("ref")
    if ref is not None:
        ref = str(ref).strip()
        if not ref:
            raise MarketplaceYmlError(f"'packages[{index}].ref' must be a non-empty string")

    if not is_local and version is None and ref is None:
        raise MarketplaceYmlError(
            f"packages[{index}] ('{name}'): remote packages require at "
            f"least one of 'version' or 'ref'"
        )

    # APM-only: tag_pattern
    tag_pattern: str | None = raw.get("tag_pattern")
    if tag_pattern is not None:
        if not isinstance(tag_pattern, str) or not tag_pattern.strip():
            raise MarketplaceYmlError(f"'packages[{index}].tag_pattern' must be a non-empty string")
        tag_pattern = tag_pattern.strip()
        _validate_tag_pattern(tag_pattern, context=f"packages[{index}].tag_pattern")

    # APM-only: include_prerelease
    include_prerelease = raw.get("include_prerelease", False)
    if not isinstance(include_prerelease, bool):
        raise MarketplaceYmlError(f"'packages[{index}].include_prerelease' must be a boolean")

    # Anthropic pass-through: description
    description: str | None = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            raise MarketplaceYmlError(f"'packages[{index}].description' must be a non-empty string")
        description = description.strip()

    # Anthropic pass-through: homepage
    homepage: str | None = raw.get("homepage")
    if homepage is not None:
        if not isinstance(homepage, str) or not homepage.strip():
            raise MarketplaceYmlError(f"'packages[{index}].homepage' must be a non-empty string")
        homepage = homepage.strip()

    # Anthropic pass-through: tags + keywords (merged, deduplicated)
    tags = _parse_tags_and_keywords(raw, index)

    # Anthropic pass-through: author
    author = _parse_author(raw.get("author"), index)

    # Anthropic pass-through: license
    license_val: str | None = raw.get("license")
    if license_val is not None:
        if not isinstance(license_val, str) or not license_val.strip():
            raise MarketplaceYmlError(f"'packages[{index}].license' must be a non-empty string")
        license_val = license_val.strip()

    # Anthropic pass-through: repository
    repository: str | None = raw.get("repository")
    if repository is not None:
        if not isinstance(repository, str) or not repository.strip():
            raise MarketplaceYmlError(f"'packages[{index}].repository' must be a non-empty string")
        repository = repository.strip()

    # Marketplace category
    category: str | None = None
    raw_category = raw.get("category")
    if raw_category is not None:
        if not isinstance(raw_category, str) or not raw_category.strip():
            raise MarketplaceYmlError(f"'packages[{index}].category' must be a non-empty string")
        category = raw_category.strip()

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
        host=host,
    )


def _parse_tags_and_keywords(raw: dict[str, Any], index: int) -> tuple[str, ...]:
    """Parse and merge ``tags`` and ``keywords`` fields, capped per S4."""
    raw_tags = raw.get("tags")
    tags: tuple[str, ...] = ()
    if raw_tags is not None:
        if not isinstance(raw_tags, list):
            raise MarketplaceYmlError(f"'packages[{index}].tags' must be a list of strings")
        for i, item in enumerate(raw_tags):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'packages[{index}].tags[{i}]' must be a string, got {type(item).__name__}"
                )
        tags = tuple(str(t) for t in raw_tags)

    raw_keywords = raw.get("keywords")
    if raw_keywords is not None:
        if not isinstance(raw_keywords, list):
            raise MarketplaceYmlError(f"'packages[{index}].keywords' must be a list of strings")
        for i, item in enumerate(raw_keywords):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'packages[{index}].keywords[{i}]' must be a string, got {type(item).__name__}"
                )
        seen = set(tags)
        merged = list(tags)
        for kw in raw_keywords:
            if kw not in seen:
                seen.add(kw)
                merged.append(kw)
        tags = tuple(merged)

    # S4: cap array length and item length
    if len(tags) > _MAX_TAGS_COUNT:
        import logging as _lg

        _lg.getLogger(__name__).warning(
            "packages[%d]: tags truncated from %d to %d items",
            index,
            len(tags),
            _MAX_TAGS_COUNT,
        )
        tags = tags[:_MAX_TAGS_COUNT]
    return tuple(t[:_MAX_TAG_LENGTH] for t in tags)


# ---------------------------------------------------------------------------
# Config field assembler (shared by both loaders via yml_schema._build_config)
# ---------------------------------------------------------------------------


def _build_config_fields(
    marketplace_dict: dict[str, Any],
    default_output: str,
    warnings_sink: list[str],
) -> tuple[
    MarketplaceOwner,
    tuple[str, ...],
    tuple[MarketplaceOutputSpec, ...],
    str,
    MarketplaceClaudeConfig,
    dict[str, Any],
    MarketplaceBuild,
    MarketplaceVersioning,
    MarketplaceCodexConfig,
]:
    """Parse all sub-blocks from *marketplace_dict*.

    Returns ``(owner, outputs, output_specs, output, claude,
    metadata, build, versioning, codex)``.  The ``output`` string
    is already path-traversal-checked.
    """
    # owner
    raw_owner = marketplace_dict.get("owner")
    if raw_owner is None:
        raise MarketplaceYmlError("'owner' is required")
    owner = _parse_owner(raw_owner)

    # output selection
    outputs, output_specs = _parse_outputs(
        marketplace_dict.get("outputs"), warnings_sink=warnings_sink
    )

    # Claude output -- legacy shorthand ``output:`` is the default_output
    legacy_output = marketplace_dict.get("output")
    output = default_output if legacy_output is None else legacy_output
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'output' must be a non-empty string")
    output = output.strip()

    # Path traversal guard for raw ``output`` value
    try:
        validate_path_segments(output, context="marketplace output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    claude = _parse_claude(marketplace_dict.get("claude"), default_output=output)
    # After parse_claude the canonical output is claude.output
    output = claude.output

    # metadata (Anthropic pass-through, preserve verbatim)
    metadata: dict[str, Any] = {}
    raw_metadata = marketplace_dict.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict):
            raise MarketplaceYmlError("'metadata' must be a mapping")
        metadata = dict(raw_metadata)

    build = _parse_build(marketplace_dict.get("build"))
    versioning = _parse_versioning(marketplace_dict.get("versioning"))
    codex = _parse_codex(marketplace_dict.get("codex"))

    return owner, outputs, output_specs, output, claude, metadata, build, versioning, codex


# ---------------------------------------------------------------------------
# YAML file reader (shared by both loaders)
# ---------------------------------------------------------------------------


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read *path* and return its top-level mapping or raise."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketplaceYmlError(f"Cannot read '{path}': {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = ""
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            mark = exc.problem_mark
            detail = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise MarketplaceYmlError(f"YAML parse error in '{path}'{detail}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise MarketplaceYmlError(f"'{path}' must contain a YAML mapping at the top level")
    return data
