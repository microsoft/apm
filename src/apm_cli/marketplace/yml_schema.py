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
from typing import Any, Dict, List, Mapping, Optional, Tuple  # noqa: F401, UP035

import yaml

from ..utils.path_security import PathTraversalError, validate_path_segments
from .errors import MarketplaceYmlError

__all__ = [
    "LOCAL_SOURCE_RE",
    "SOURCE_RE",
    "DirectPackageEntry",
    "MarketplaceBuild",
    "MarketplaceConfig",
    "MarketplaceOwner",
    "MarketplacePackage",
    "MarketplaceYml",  # backwards-compat alias
    "MarketplaceYmlError",
    "PackageEntry",
    "Upstream",
    "UpstreamPackageEntry",
    "load_marketplace_from_apm_yml",
    "load_marketplace_from_legacy_yml",
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

# Source field accepts either ``owner/repo`` (remote) or ``./...`` (local
# path within the same repo).  Used by both yml_schema and yml_editor for
# source field validation.
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")
# Remote-only ``owner/repo`` shape -- used by upstream registration
# validation, where local-path shorthand is not meaningful. Disallows a
# leading ``.`` so ``./local`` is rejected, matching curator intent.
# Shared with upstream_cache and upstream_parser via ref_resolver.
from .ref_resolver import OWNER_REPO_RE as _REMOTE_SOURCE_RE  # noqa: E402

# Upstream alias: identifier-like, used as a directory-safe key in cache
# paths and lockfile keys. Conservative character set keeps cache keys
# Windows-safe and avoids collisions with the ``__`` cache delimiter.
_UPSTREAM_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Hostname validation -- minimal sanity check; refers to git-host domain.
_UPSTREAM_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")

# Placeholder tokens accepted in ``tag_pattern`` / ``build.tagPattern``.
_TAG_PLACEHOLDERS = ("{version}", "{name}")

# ---------------------------------------------------------------------------
# Permitted key sets (strict mode)
# ---------------------------------------------------------------------------

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
    }
)

# Alias for the direct-package shape; used when explicitly distinguishing
# from the upstream-package shape in builder dispatch and tests.
_DIRECT_PACKAGE_KEYS = _PACKAGE_ENTRY_KEYS

# Strict key set for upstream-sourced package entries. NOTE: ``source``
# and ``subdir`` are deliberately excluded -- presence of either alongside
# ``upstream`` is an authoring error caught at parse time.
_UPSTREAM_PACKAGE_KEYS = frozenset(
    {
        "name",
        "upstream",
        "plugin",
        "version",
        "ref",
        "tag_pattern",
        "include_prerelease",
        "allow_head",
        # Anthropic pass-through overrides (curator may override
        # individual display fields without touching the upstream
        # plugin's content).
        "description",
        "homepage",
        "tags",
        "keywords",
        "author",
        "license",
        "repository",
    }
)

# Strict key set for entries inside the ``upstreams:`` block.
_UPSTREAM_REGISTRATION_KEYS = frozenset(
    {
        "alias",
        "repo",
        "path",
        "ref",
        "branch",
        "host",
        "allow_head",
    }
)

# Limits for keywords/tags array to prevent DoS via oversized manifests (S4).
_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100

# Keys permitted inside an ``author`` object (rejected if anything else
# present). Mirrors the Claude Code plugin manifest schema.
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})


def _parse_author(raw: Any, index: int) -> dict[str, str] | None:
    """Normalize a curator-supplied ``author`` value to a Claude-Code-
    compliant object ``{name, email?, url?}``.

    Accepts either a non-empty string (treated as ``name``) or a mapping
    with at least ``name`` and only the permitted keys. Returns ``None``
    when ``raw`` is ``None``. Raises :class:`MarketplaceYmlError` on any
    other shape.
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


# Keys permitted inside the ``marketplace:`` block of apm.yml.  This is
# distinct from the legacy top-level keys (which include ``name``,
# ``description``, ``version`` -- those are inherited from apm.yml's
# top-level scalars in the new world).
_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",  # optional override of top-level apm.yml name
        "description",  # optional override of top-level apm.yml description
        "version",  # optional override of top-level apm.yml version
        "owner",
        "output",
        "metadata",
        "build",
        "packages",
        "upstreams",
    }
)
# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketplaceOwner:
    """Owner block of ``marketplace.yml``."""

    name: str
    email: str | None = None
    url: str | None = None


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
    subdir: str | None = None
    version: str | None = None
    ref: str | None = None
    tag_pattern: str | None = None
    include_prerelease: bool = False
    # Anthropic pass-through fields
    description: str | None = None
    homepage: str | None = None
    tags: tuple[str, ...] = ()
    # ``author`` is normalized to a Claude-Code-compliant object:
    # ``{"name": str, "email"?: str, "url"?: str}``. Accepts either a
    # bare string (treated as ``name``) or a mapping at parse time.
    author: Mapping[str, str] | None = None
    license: str | None = None
    repository: str | None = None
    # Derived (set by loader, not by user)
    is_local: bool = False


# Alias for explicit naming in dispatch / tests; identical to PackageEntry.
DirectPackageEntry = PackageEntry


@dataclass(frozen=True)
class Upstream:
    """A registered external marketplace pointer.

    Declared in ``apm.yml -> marketplace.upstreams[]``. Each entry is
    addressed by its ``alias`` from upstream-sourced ``packages[]``
    entries (via the ``upstream:`` field).

    Attributes
    ----------
    alias : str
        Curator-chosen identifier; must match
        ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}`` so it is safe to use as a
        directory-segment in cache paths and as a lockfile key.
    repo : str
        ``owner/repo`` of the upstream marketplace.
    path : str
        Path to the upstream ``marketplace.json`` within ``repo``.
        Defaults to ``.claude-plugin/marketplace.json``.
    ref : str | None
        Pinned commit SHA or tag for the upstream manifest. Required
        for reproducible builds; if absent, ``allow_head`` must be
        ``True`` and ``branch`` HEAD is used (with a build warning).
    branch : str
        Branch tracked when ``ref`` is absent and ``allow_head`` is
        ``True``. Defaults to ``main``.
    host : str
        Git host. Defaults to ``github.com``.
    allow_head : bool
        Curator opt-in to track a moving branch HEAD. Default ``False``.
        Governance can forbid via the
        ``marketplace.upstream.allow_unpinned_refs`` policy key.
    """

    alias: str
    repo: str
    path: str = ".claude-plugin/marketplace.json"
    ref: str | None = None
    branch: str = "main"
    host: str = "github.com"
    allow_head: bool = False


@dataclass(frozen=True)
class UpstreamPackageEntry:
    """A ``packages[]`` entry sourced from a registered upstream.

    Distinguished from :class:`PackageEntry` (the direct shape) by the
    presence of the ``upstream`` field and the absence of ``source``.
    The builder dispatches on ``isinstance``; mixing both shapes in the
    same ``packages:`` list is supported.

    Attributes
    ----------
    name : str
        Display name in the curator's emitted ``marketplace.json``. May
        differ from ``plugin`` when the curator renames.
    upstream_alias : str
        References ``upstreams[].alias``. The YAML key is ``upstream``;
        the internal attribute is renamed because ``upstream`` would
        clash with future API method names and to make the alias-vs-
        plugin-name distinction explicit at use sites.
    plugin : str | None
        Name of the plugin in the upstream marketplace. Defaults to
        ``name`` when absent.
    version : str | None
        Optional curator-supplied semver range (mutually exclusive with
        ``ref``). Resolved against the upstream plugin's repo tags.
    ref : str | None
        Optional curator-supplied commit SHA or tag (overrides the
        upstream plugin's pinned ref).
    tag_pattern : str | None
        Optional curator override of ``build.tagPattern`` for this
        package's version resolution.
    include_prerelease : bool
        Curator override; default ``False``.
    allow_head : bool
        Per-entry opt-in to mutable refs. Default ``False``.
    description, homepage, tags, author, license, repository :
        Optional Anthropic pass-through overrides; if absent, the
        upstream plugin's values pass through verbatim.
    """

    name: str
    upstream_alias: str
    plugin: str | None = None
    # APM-only fields (overrides; mutually exclusive `version`/`ref`)
    version: str | None = None
    ref: str | None = None
    tag_pattern: str | None = None
    include_prerelease: bool = False
    allow_head: bool = False
    # Anthropic pass-through overrides
    description: str | None = None
    homepage: str | None = None
    tags: tuple[str, ...] = ()
    author: Mapping[str, str] | None = None
    license: str | None = None
    repository: str | None = None


# Public union alias for the heterogeneous ``packages`` collection. The
# builder dispatches on ``isinstance`` to pick the correct emit path.
MarketplacePackage = PackageEntry | UpstreamPackageEntry


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
    metadata: dict[str, Any] = field(default_factory=dict)
    build: MarketplaceBuild = field(default_factory=MarketplaceBuild)
    packages: tuple[MarketplacePackage, ...] = ()
    upstreams: tuple[Upstream, ...] = ()
    # Origin tracking + override-detection metadata
    source_path: Path | None = None
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


# ---------------------------------------------------------------------------
# Internal parse helpers
# ---------------------------------------------------------------------------


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


def _parse_package_entry(raw: Any, index: int) -> MarketplacePackage:
    """Dispatch to the direct-package or upstream-package parser.

    Discriminates on the presence of ``source`` vs ``upstream``. Both
    keys present, or neither, is a hard error caught here so the
    individual shape parsers can apply their own strict key sets.
    """
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"packages[{index}] must be a mapping")

    has_source = "source" in raw and raw["source"] is not None
    has_upstream = "upstream" in raw and raw["upstream"] is not None

    if has_source and has_upstream:
        raise MarketplaceYmlError(
            f"packages[{index}]: 'source' and 'upstream' are mutually "
            f"exclusive (a package is either a direct git source or "
            f"sourced from a registered upstream, never both)"
        )
    if not has_source and not has_upstream:
        raise MarketplaceYmlError(
            f"packages[{index}]: requires exactly one of 'source' "
            f"(direct) or 'upstream' (upstream-sourced)"
        )

    if has_upstream:
        return _parse_upstream_package_entry(raw, index)
    return _parse_direct_package_entry(raw, index)


def _parse_direct_package_entry(raw: dict[str, Any], index: int) -> PackageEntry:
    """Parse and validate a single direct ``packages`` entry."""
    # -- strict key check --
    _check_unknown_keys(raw, _DIRECT_PACKAGE_KEYS, context=f"packages[{index}]")

    name = _require_str(raw, "name", context=f"packages[{index}]")
    source = _require_str(raw, "source", context=f"packages[{index}]")
    _validate_source(source, index=index)
    is_local = bool(LOCAL_SOURCE_RE.match(source))

    # APM-only: subdir (irrelevant for local packages but harmless)
    subdir: str | None = raw.get("subdir")
    if subdir is not None:
        if not isinstance(subdir, str) or not subdir.strip():
            raise MarketplaceYmlError(f"'packages[{index}].subdir' must be a non-empty string")
        subdir = subdir.strip()
        try:
            validate_path_segments(subdir, context=f"packages[{index}].subdir")
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc

    # APM-only: version (semver range -- stored as string, not parsed here)
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

    # At least one of version or ref must be present for REMOTE packages.
    # Local-path packages skip git resolution so the requirement does not
    # apply to them.
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

    # Anthropic pass-through: tags
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

    # Anthropic pass-through: keywords (alias for tags -- merged, deduplicated)
    raw_keywords = raw.get("keywords")
    if raw_keywords is not None:
        if not isinstance(raw_keywords, list):
            raise MarketplaceYmlError(f"'packages[{index}].keywords' must be a list of strings")
        for i, item in enumerate(raw_keywords):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'packages[{index}].keywords[{i}]' must be a string, got {type(item).__name__}"
                )
        # Merge: tags first, then keywords entries (deduplicated)
        seen = set(tags)
        merged = list(tags)
        for kw in raw_keywords:
            if kw not in seen:
                seen.add(kw)
                merged.append(kw)
        tags = tuple(merged)

    # S4: cap tags array length and item length
    if len(tags) > _MAX_TAGS_COUNT:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "packages[%d] ('%s'): tags truncated from %d to %d items",
            index,
            name,
            len(tags),
            _MAX_TAGS_COUNT,
        )
        tags = tags[:_MAX_TAGS_COUNT]
    tags = tuple(t[:_MAX_TAG_LENGTH] for t in tags)

    # Anthropic pass-through: author -- accept string OR object input,
    # normalize to ``{name, email?, url?}`` per the Claude Code plugin
    # manifest schema (json.schemastore.org/claude-code-plugin-manifest.json).
    author = _parse_author(raw.get("author"), index)

    # Anthropic pass-through: license (S3 -- must be str)
    license_val: str | None = raw.get("license")
    if license_val is not None:
        if not isinstance(license_val, str) or not license_val.strip():
            raise MarketplaceYmlError(f"'packages[{index}].license' must be a non-empty string")
        license_val = license_val.strip()

    # Anthropic pass-through: repository (S3 -- must be str)
    repository: str | None = raw.get("repository")
    if repository is not None:
        if not isinstance(repository, str) or not repository.strip():
            raise MarketplaceYmlError(f"'packages[{index}].repository' must be a non-empty string")
        repository = repository.strip()

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
        is_local=is_local,
    )


def _parse_upstream_package_entry(raw: dict[str, Any], index: int) -> UpstreamPackageEntry:
    """Parse and validate a single upstream-sourced ``packages`` entry.

    The strict key set forbids ``source`` and ``subdir`` -- their
    presence on an upstream entry is an authoring error caught here.
    Cross-validation that ``upstream`` references a declared alias
    happens later in :func:`_build_config` once the ``upstreams:``
    block has been parsed.
    """
    ctx = f"packages[{index}]"
    _check_unknown_keys(raw, _UPSTREAM_PACKAGE_KEYS, context=ctx)

    name = _require_str(raw, "name", context=ctx)
    upstream_alias = _require_str(raw, "upstream", context=ctx)
    if not _UPSTREAM_ALIAS_RE.match(upstream_alias):
        raise MarketplaceYmlError(
            f"'{ctx}.upstream' '{upstream_alias}' is not a valid alias "
            f"(allowed: leading alphanumeric, then alphanumerics/'_'/'-', "
            f"max 64 chars)"
        )

    plugin: str | None = raw.get("plugin")
    if plugin is not None:
        if not isinstance(plugin, str) or not plugin.strip():
            raise MarketplaceYmlError(f"'{ctx}.plugin' must be a non-empty string")
        plugin = plugin.strip()

    # APM-only: version (semver range, mutually exclusive with ref)
    version: str | None = raw.get("version")
    if version is not None:
        version = str(version).strip()
        if not version:
            raise MarketplaceYmlError(f"'{ctx}.version' must be a non-empty string")

    # APM-only: ref (commit SHA or tag)
    ref: str | None = raw.get("ref")
    if ref is not None:
        ref = str(ref).strip()
        if not ref:
            raise MarketplaceYmlError(f"'{ctx}.ref' must be a non-empty string")

    if version is not None and ref is not None:
        raise MarketplaceYmlError(
            f"'{ctx}': 'version' and 'ref' are mutually exclusive "
            f"(precedence: explicit ref wins; specify only one)"
        )

    # APM-only: tag_pattern
    tag_pattern: str | None = raw.get("tag_pattern")
    if tag_pattern is not None:
        if not isinstance(tag_pattern, str) or not tag_pattern.strip():
            raise MarketplaceYmlError(f"'{ctx}.tag_pattern' must be a non-empty string")
        tag_pattern = tag_pattern.strip()
        _validate_tag_pattern(tag_pattern, context=f"{ctx}.tag_pattern")

    # APM-only: include_prerelease
    include_prerelease = raw.get("include_prerelease", False)
    if not isinstance(include_prerelease, bool):
        raise MarketplaceYmlError(f"'{ctx}.include_prerelease' must be a boolean")

    # APM-only: allow_head (per-entry opt-in to mutable refs)
    allow_head = raw.get("allow_head", False)
    if not isinstance(allow_head, bool):
        raise MarketplaceYmlError(f"'{ctx}.allow_head' must be a boolean")

    # Anthropic pass-through overrides (all optional)
    description: str | None = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            raise MarketplaceYmlError(f"'{ctx}.description' must be a non-empty string")
        description = description.strip()

    homepage: str | None = raw.get("homepage")
    if homepage is not None:
        if not isinstance(homepage, str) or not homepage.strip():
            raise MarketplaceYmlError(f"'{ctx}.homepage' must be a non-empty string")
        homepage = homepage.strip()

    raw_tags = raw.get("tags")
    tags: tuple[str, ...] = ()
    if raw_tags is not None:
        if not isinstance(raw_tags, list):
            raise MarketplaceYmlError(f"'{ctx}.tags' must be a list of strings")
        for i, item in enumerate(raw_tags):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'{ctx}.tags[{i}]' must be a string, got {type(item).__name__}"
                )
        tags = tuple(str(t) for t in raw_tags)

    raw_keywords = raw.get("keywords")
    if raw_keywords is not None:
        if not isinstance(raw_keywords, list):
            raise MarketplaceYmlError(f"'{ctx}.keywords' must be a list of strings")
        for i, item in enumerate(raw_keywords):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'{ctx}.keywords[{i}]' must be a string, got {type(item).__name__}"
                )
        seen = set(tags)
        merged = list(tags)
        for kw in raw_keywords:
            if kw not in seen:
                seen.add(kw)
                merged.append(kw)
        tags = tuple(merged)

    # S4: cap tags array length and item length (mirror direct shape).
    if len(tags) > _MAX_TAGS_COUNT:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "packages[%d] ('%s'): tags truncated from %d to %d items",
            index,
            name,
            len(tags),
            _MAX_TAGS_COUNT,
        )
        tags = tags[:_MAX_TAGS_COUNT]
    tags = tuple(t[:_MAX_TAG_LENGTH] for t in tags)

    author = _parse_author(raw.get("author"), index)

    license_val: str | None = raw.get("license")
    if license_val is not None:
        if not isinstance(license_val, str) or not license_val.strip():
            raise MarketplaceYmlError(f"'{ctx}.license' must be a non-empty string")
        license_val = license_val.strip()

    repository: str | None = raw.get("repository")
    if repository is not None:
        if not isinstance(repository, str) or not repository.strip():
            raise MarketplaceYmlError(f"'{ctx}.repository' must be a non-empty string")
        repository = repository.strip()

    return UpstreamPackageEntry(
        name=name,
        upstream_alias=upstream_alias,
        plugin=plugin,
        version=version,
        ref=ref,
        tag_pattern=tag_pattern,
        include_prerelease=include_prerelease,
        allow_head=allow_head,
        description=description,
        homepage=homepage,
        tags=tags,
        author=author,
        license=license_val,
        repository=repository,
    )


def _parse_upstream_registration(raw: Any, index: int) -> Upstream:
    """Parse and validate a single ``upstreams[]`` block entry."""
    ctx = f"upstreams[{index}]"
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"{ctx} must be a mapping")

    _check_unknown_keys(raw, _UPSTREAM_REGISTRATION_KEYS, context=ctx)

    alias = _require_str(raw, "alias", context=ctx)
    if not _UPSTREAM_ALIAS_RE.match(alias):
        raise MarketplaceYmlError(
            f"'{ctx}.alias' '{alias}' is not a valid alias "
            f"(allowed: leading alphanumeric, then alphanumerics/'_'/'-', "
            f"max 64 chars)"
        )

    repo = _require_str(raw, "repo", context=ctx)
    if not _REMOTE_SOURCE_RE.match(repo):
        raise MarketplaceYmlError(f"'{ctx}.repo' must match '<owner>/<repo>' shape, got '{repo}'")

    path = raw.get("path")
    if path is None:
        path = ".claude-plugin/marketplace.json"
    if not isinstance(path, str) or not path.strip():
        raise MarketplaceYmlError(f"'{ctx}.path' must be a non-empty string")
    path = path.strip()
    try:
        validate_path_segments(path, context=f"{ctx}.path")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    ref: str | None = raw.get("ref")
    if ref is not None:
        ref = str(ref).strip()
        if not ref:
            raise MarketplaceYmlError(f"'{ctx}.ref' must be a non-empty string")

    branch = raw.get("branch")
    if branch is None:
        branch = "main"
    if not isinstance(branch, str) or not branch.strip():
        raise MarketplaceYmlError(f"'{ctx}.branch' must be a non-empty string")
    branch = branch.strip()

    host = raw.get("host")
    if host is None:
        host = "github.com"
    if not isinstance(host, str) or not host.strip():
        raise MarketplaceYmlError(f"'{ctx}.host' must be a non-empty string")
    host = host.strip()
    if not _UPSTREAM_HOST_RE.match(host):
        raise MarketplaceYmlError(f"'{ctx}.host' '{host}' is not a valid hostname")

    allow_head = raw.get("allow_head", False)
    if not isinstance(allow_head, bool):
        raise MarketplaceYmlError(f"'{ctx}.allow_head' must be a boolean")

    # Reproducibility guard: a missing ref is only acceptable if the
    # author explicitly opts in via ``allow_head: true``. The builder
    # additionally emits a ``BuildDiagnostic(level="warn")`` whenever
    # an upstream is resolved against branch HEAD; governance can
    # forbid it via the ``marketplace.upstream.allow_unpinned_refs``
    # policy key.
    if ref is None and not allow_head:
        raise MarketplaceYmlError(
            f"'{ctx}': 'ref' is required for reproducible builds. "
            f"Set ref to a commit SHA or tag, or set "
            f"'allow_head: true' to opt in to tracking branch HEAD "
            f"(builds will warn)."
        )

    return Upstream(
        alias=alias,
        repo=repo,
        path=path,
        ref=ref,
        branch=branch,
        host=host,
        allow_head=allow_head,
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
        raise MarketplaceYmlError("'marketplace' in apm.yml must be a mapping")

    # -- strict marketplace-block key check --
    _check_unknown_keys(raw_block, _APM_MARKETPLACE_KEYS, context="marketplace")

    # -- inheritance with optional overrides --
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
    else:  # noqa: PLR5501
        if not isinstance(top_desc, str) or not top_desc.strip():
            description = ""
        else:
            description = top_desc.strip()

    if ver_overridden:
        version_str = _require_str(raw_block, "version", context="marketplace")
    else:  # noqa: PLR5501
        if top_ver is None:  # noqa: SIM108
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
        raise MarketplaceYmlError("'output' must be a non-empty string")
    output = output.strip()

    # Path-traversal guard -- reject output paths containing ".." segments.
    try:
        validate_path_segments(output, context="marketplace output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    # -- metadata (Anthropic pass-through, preserve verbatim) --
    metadata: dict[str, Any] = {}
    raw_metadata = marketplace_dict.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict):
            raise MarketplaceYmlError("'metadata' must be a mapping")
        metadata = dict(raw_metadata)

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

    # -- build --
    build = _parse_build(marketplace_dict.get("build"))

    # -- upstreams (optional) -- parse FIRST so packages cross-validation
    # can verify each upstream-sourced entry references a declared alias.
    raw_upstreams = marketplace_dict.get("upstreams")
    if raw_upstreams is None:
        raw_upstreams = []
    if not isinstance(raw_upstreams, list):
        raise MarketplaceYmlError("'upstreams' must be a list")

    upstream_entries: list[Upstream] = []
    seen_aliases: dict[str, int] = {}
    for u_idx, raw_upstream in enumerate(raw_upstreams):
        upstream = _parse_upstream_registration(raw_upstream, u_idx)
        if upstream.alias in seen_aliases:
            raise MarketplaceYmlError(
                f"Duplicate upstream alias '{upstream.alias}' "
                f"(upstreams[{seen_aliases[upstream.alias]}] and "
                f"upstreams[{u_idx}])"
            )
        seen_aliases[upstream.alias] = u_idx
        upstream_entries.append(upstream)

    declared_aliases = {u.alias for u in upstream_entries}

    # -- packages --
    raw_packages = marketplace_dict.get("packages")
    if raw_packages is None:
        raw_packages = []
    if not isinstance(raw_packages, list):
        raise MarketplaceYmlError("'packages' must be a list")

    entries: list[MarketplacePackage] = []
    seen_names: dict[str, int] = {}
    seen_upstream_pairs: dict[tuple[str, str], int] = {}
    for idx, raw_entry in enumerate(raw_packages):
        entry = _parse_package_entry(raw_entry, idx)
        # Cross-shape `name` uniqueness (panel: prevents dependency-
        # confusion / name shadowing across direct + upstream).
        lower_name = entry.name.lower()
        if lower_name in seen_names:
            raise MarketplaceYmlError(
                f"Duplicate package name '{entry.name}' "
                f"(packages[{seen_names[lower_name]}] and packages[{idx}])"
            )
        seen_names[lower_name] = idx

        if isinstance(entry, UpstreamPackageEntry):
            # Cross-validate against declared upstreams.
            if entry.upstream_alias not in declared_aliases:
                known = ", ".join(sorted(declared_aliases)) or "(none declared)"
                raise MarketplaceYmlError(
                    f"packages[{idx}] ('{entry.name}'): "
                    f"upstream '{entry.upstream_alias}' is not declared "
                    f"in marketplace.upstreams (known aliases: {known})"
                )
            # Reject duplicate (upstream_alias, plugin) pairs -- the
            # same upstream plugin cannot be exposed twice under
            # different display names without explicit operator intent.
            plugin_key = entry.plugin or entry.name
            pair = (entry.upstream_alias, plugin_key)
            if pair in seen_upstream_pairs:
                prev = seen_upstream_pairs[pair]
                raise MarketplaceYmlError(
                    f"Duplicate upstream package "
                    f"({entry.upstream_alias}/{plugin_key}) "
                    f"(packages[{prev}] and packages[{idx}])"
                )
            seen_upstream_pairs[pair] = idx

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
        upstreams=tuple(upstream_entries),
        source_path=source_path,
        is_legacy=is_legacy,
        name_overridden=name_overridden,
        description_overridden=description_overridden,
        version_overridden=version_overridden,
    )
