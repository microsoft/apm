"""Strict parser for upstream ``marketplace.json`` manifests.

Counterpart to :func:`apm_cli.marketplace.models.parse_marketplace_json`,
which is *lenient*: it silently skips unrecognised entries to maximise
compatibility with both Copilot CLI and Claude Code formats. That
behaviour is correct for the consumer-side ``apm marketplace browse``
flow, but wrong for the curator-side ``upstream`` resolution where every
silent skip is a build-time foot-gun (an entry the curator thinks is
exposed but is not).

This module provides the strict alternative used by
:class:`UpstreamResolver`:

- Every supported source shape is parsed into a fully-resolved
  :class:`StrictPluginSource` -- no ambiguity for downstream resolvers.
- Every unsupported entry produces a named :class:`StrictRejection`
  instead of being dropped. The builder maps these to
  ``BuildDiagnostic(level="error")`` so curators see exactly which
  entry was refused and why.
- String-shorthand sources (``./foo`` / ``foo``) are resolved as
  subdirectories of the upstream marketplace's repo, gated by
  ``metadata.pluginRoot`` and validated against path traversal via
  :func:`apm_cli.utils.path_security.validate_path_segments`.

Contract
--------

The strict parser only accepts entries it is sure how to resolve to an
immutable, single-host, git-backed coordinate. v1 supports the
``github`` host family and the following source shapes:

* ``repository: "owner/repo"`` (Copilot CLI shape) plus optional
  ``ref``/``sha``.
* ``source: {type: "github", repo: "owner/repo", ref?, sha?}``.
* ``source: {type: "git-subdir", repo|url: "owner/repo", path,
  ref?, sha?}`` -- subdir is preserved verbatim and validated.
* ``source: "./foo"`` or ``"foo"`` (string shorthand) -- resolved as a
  subdirectory of the upstream repo. The resolved subdir must live
  under ``metadata.pluginRoot`` (or under the repo root if
  ``pluginRoot`` is unset). ``..`` segments are rejected.

Anything else (``npm``, arbitrary URLs, non-github hosts, malformed
``owner/repo``) is a :class:`StrictRejection`. Rejections do **not**
raise; they are returned in :class:`StrictManifest.rejections` so the
caller can decide whether a single bad entry should fail the entire
build or merely surface as a diagnostic.

Cross-references
----------------

* Plan: ``~/.copilot/session-state/.../plan.md`` -- "Builder behaviour
  (atomic, deterministic, strict)" + "Source-shape support matrix".
* Schema: :mod:`apm_cli.marketplace.yml_schema` --
  :class:`UpstreamPackageEntry` references the alias which carries the
  upstream coordinates this parser needs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

logger = logging.getLogger(__name__)


__all__ = [
    "REJECTION_REASONS",
    "StrictManifest",
    "StrictPlugin",
    "StrictPluginSource",
    "StrictRejection",
    "parse_marketplace_strict",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ``owner/repo`` shape. Reuses the regex from yml_schema with the same
# leading-dot rejection so ``./local`` cannot pose as a remote source.
_REMOTE_SOURCE_RE = re.compile(r"^[^./\s][^/\s]*/[^/\s]+$")

# 40-char lowercase hex git SHA. Truncated SHAs are explicitly rejected
# because the strict parser is the line of defence against ambiguous
# refs reaching ``RefResolver``. Aliased to the shared canonical pattern
# in ``ref_resolver`` so authoring + parsing share one source of truth.
from .ref_resolver import FULL_SHA_RE as _FULL_SHA_RE  # noqa: E402

# Tag / ref names: conservative subset. Disallows whitespace, tilde,
# caret, and other characters that ``git ls-remote`` would refuse, but
# permits the dots/slashes/dashes that real release tags use.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-+]{0,254}$")

# v1 host allow-list. Cross-host plugins (upstream on github, plugin on
# gitlab) are explicitly out of scope -- the plan reserves multi-host
# for v2 to avoid expanding the auth surface area in this slice.
_SUPPORTED_HOSTS: frozenset[str] = frozenset({"github.com"})

# Strict per-shape key sets. Anything outside these is a ``unknown-key``
# rejection so curators learn about typos at strict-parse time rather
# than seeing the entry silently dropped by the lenient consumer
# parser.
_GITHUB_SOURCE_KEYS: frozenset[str] = frozenset({"type", "host", "repo", "ref", "sha", "branch"})
_GIT_SUBDIR_SOURCE_KEYS: frozenset[str] = frozenset(
    {"type", "host", "repo", "url", "path", "ref", "sha", "branch"}
)

# Top-level plugin entry keys we recognise. The lenient parser accepts
# arbitrary metadata; the strict parser tolerates display-only fields
# (``description``, ``version``, ``tags``, ``homepage``, ``license``,
# ``author``, ``authors``, ``keywords``, ``category``, ``categories``)
# but rejects anything not on this list to surface typos like
# ``soruce``.
_PLUGIN_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "source",
        "repository",
        "ref",
        "sha",
        "description",
        "version",
        "tags",
        "homepage",
        "license",
        "author",
        "authors",
        "keywords",
        "category",
        "categories",
    }
)


# Public catalogue of rejection reasons. Documented for callers (CLI
# diagnostics, tests) so the named-reason contract is part of the
# module API.
REJECTION_REASONS: frozenset[str] = frozenset(
    {
        "missing-name",
        "duplicate-name",
        "missing-source",
        "ambiguous-source",
        "invalid-source-shape",
        "unknown-source-key",
        "unknown-plugin-key",
        "unsupported-source-type",
        "npm-source",
        "invalid-repo",
        "unsupported-host",
        "missing-subdir",
        "relative-source-out-of-root",
        "path-traversal",
        "invalid-ref",
        "invalid-sha",
    }
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrictPluginSource:
    """Fully-resolved upstream plugin source coordinates.

    Always carries enough information for :class:`RefResolver` to
    produce an immutable commit SHA without re-parsing the upstream
    manifest. ``ref`` and ``sha`` are mutually optional but at least
    one is set when the upstream pinned its plugin; if both are unset
    the resolver MUST treat the entry as unpinned and apply the
    precedence ladder (curator override, then upstream-marketplace
    pinned ref).
    """

    type: str  # "github" | "git-subdir"
    host: str  # always in _SUPPORTED_HOSTS
    repo: str  # "owner/repo"
    ref: str | None = None
    sha: str | None = None
    subdir: str | None = None  # subdirectory within the repo, if any


@dataclass(frozen=True)
class StrictPlugin:
    """A strictly-parsed upstream plugin entry."""

    name: str
    source: StrictPluginSource
    description: str = ""
    version: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrictRejection:
    """A named rejection of a single upstream plugin entry.

    ``reason`` is one of :data:`REJECTION_REASONS` -- callers may
    pattern-match for diagnostics. ``detail`` is a human-readable
    explanation suitable for surfacing in CLI output.
    """

    plugin_name: str  # may be "<unnamed>" when name is missing/blank
    reason: str
    detail: str


@dataclass(frozen=True)
class StrictManifest:
    """Result of strict-parsing an upstream ``marketplace.json``."""

    name: str
    plugins: tuple[StrictPlugin, ...] = ()
    rejections: tuple[StrictRejection, ...] = ()
    plugin_root: str = ""
    owner_name: str = ""
    description: str = ""

    def find_plugin(self, plugin_name: str) -> StrictPlugin | None:
        """Look up a plugin by exact name (case-sensitive).

        Strict lookup intentionally avoids the lenient parser's
        case-insensitive match: the curator declared ``plugin: <name>``
        verbatim and a typo should fail loudly.
        """
        for p in self.plugins:
            if p.name == plugin_name:
                return p
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_marketplace_strict(
    data: dict[str, Any],
    *,
    upstream_owner_repo: str,
    upstream_host: str = "github.com",
) -> StrictManifest:
    """Strict-parse a fetched upstream ``marketplace.json``.

    Parameters
    ----------
    data
        The decoded JSON content of the upstream manifest.
    upstream_owner_repo
        ``owner/repo`` of the repository hosting the upstream
        marketplace. Used to resolve string-shorthand sources whose
        repo is implicit.
    upstream_host
        Host of the upstream repository. v1 only accepts hosts in
        :data:`_SUPPORTED_HOSTS`.

    Returns
    -------
    StrictManifest
        Parsed manifest with ``plugins`` (accepted entries) and
        ``rejections`` (named per-entry refusals). The caller decides
        whether any rejection should fail the build.
    """
    if not isinstance(data, dict):
        raise TypeError(
            f"upstream marketplace.json root must be a JSON object, got {type(data).__name__}"
        )

    if upstream_host not in _SUPPORTED_HOSTS:
        # Surface as a single rejection so the caller still gets a
        # StrictManifest skeleton and can format the diagnostic
        # uniformly.
        return StrictManifest(
            name=str(data.get("name", "")),
            rejections=(
                StrictRejection(
                    plugin_name="<manifest>",
                    reason="unsupported-host",
                    detail=(
                        f"upstream host '{upstream_host}' is not supported in v1 "
                        f"(supported: {sorted(_SUPPORTED_HOSTS)})"
                    ),
                ),
            ),
        )

    if not _REMOTE_SOURCE_RE.match(upstream_owner_repo):
        raise ValueError(
            f"upstream_owner_repo must be in 'owner/repo' shape, got '{upstream_owner_repo}'"
        )

    manifest_name = str(data.get("name", "")).strip()
    description = str(data.get("description", ""))

    # ``owner`` may be a string or a {name, email} dict in real
    # marketplaces (Claude Code uses the dict shape). Tolerate both.
    raw_owner = data.get("owner", "")
    if isinstance(raw_owner, dict):
        owner_name = str(raw_owner.get("name", ""))
    elif isinstance(raw_owner, str):
        owner_name = raw_owner
    else:
        owner_name = ""

    metadata = data.get("metadata", {})
    plugin_root = ""
    if isinstance(metadata, dict):
        raw_root = metadata.get("pluginRoot", "")
        if isinstance(raw_root, str):
            plugin_root = raw_root.strip().lstrip("./")

    raw_plugins = data.get("plugins", [])
    if not isinstance(raw_plugins, list):
        return StrictManifest(
            name=manifest_name,
            owner_name=owner_name,
            description=description,
            plugin_root=plugin_root,
            rejections=(
                StrictRejection(
                    plugin_name="<manifest>",
                    reason="invalid-source-shape",
                    detail="upstream marketplace.json 'plugins' must be a list",
                ),
            ),
        )

    plugins: list[StrictPlugin] = []
    rejections: list[StrictRejection] = []
    seen_names: set[str] = set()

    for entry in raw_plugins:
        if not isinstance(entry, dict):
            rejections.append(
                StrictRejection(
                    plugin_name="<unnamed>",
                    reason="invalid-source-shape",
                    detail=f"plugin entry must be a JSON object, got {type(entry).__name__}",
                )
            )
            continue
        result = _parse_strict_entry(
            entry,
            upstream_owner_repo=upstream_owner_repo,
            upstream_host=upstream_host,
            plugin_root=plugin_root,
        )
        if isinstance(result, StrictRejection):
            rejections.append(result)
            continue
        if result.name in seen_names:
            rejections.append(
                StrictRejection(
                    plugin_name=result.name,
                    reason="duplicate-name",
                    detail=(
                        f"plugin name '{result.name}' appears more than once "
                        f"in upstream marketplace"
                    ),
                )
            )
            continue
        seen_names.add(result.name)
        plugins.append(result)

    return StrictManifest(
        name=manifest_name,
        plugins=tuple(plugins),
        rejections=tuple(rejections),
        plugin_root=plugin_root,
        owner_name=owner_name,
        description=description,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_strict_entry(
    entry: dict[str, Any],
    *,
    upstream_owner_repo: str,
    upstream_host: str,
    plugin_root: str,
) -> StrictPlugin | StrictRejection:
    """Parse a single plugin entry, returning either a plugin or a rejection."""
    name = str(entry.get("name", "")).strip()
    if not name:
        return StrictRejection(
            plugin_name="<unnamed>",
            reason="missing-name",
            detail="plugin entry has no 'name' field",
        )

    unknown = set(entry.keys()) - _PLUGIN_ENTRY_KEYS
    if unknown:
        return StrictRejection(
            plugin_name=name,
            reason="unknown-plugin-key",
            detail=(
                f"plugin '{name}' has unknown key(s): {sorted(unknown)}. "
                f"Allowed: {sorted(_PLUGIN_ENTRY_KEYS)}"
            ),
        )

    has_source = "source" in entry
    has_repository = "repository" in entry
    if has_source and has_repository:
        return StrictRejection(
            plugin_name=name,
            reason="ambiguous-source",
            detail=(f"plugin '{name}' declares both 'source' and 'repository'; use exactly one"),
        )
    if not has_source and not has_repository:
        return StrictRejection(
            plugin_name=name,
            reason="missing-source",
            detail=f"plugin '{name}' has neither 'source' nor 'repository'",
        )

    description = str(entry.get("description", ""))
    version = str(entry.get("version", ""))
    raw_tags = entry.get("tags", [])
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, list):
        tags = tuple(str(t) for t in raw_tags if isinstance(t, str))

    if has_repository:
        source_or_rejection = _resolve_repository_shape(
            name=name,
            entry=entry,
            upstream_host=upstream_host,
        )
    else:
        source_or_rejection = _resolve_source_field(
            name=name,
            raw=entry["source"],
            entry=entry,
            upstream_owner_repo=upstream_owner_repo,
            upstream_host=upstream_host,
            plugin_root=plugin_root,
        )

    if isinstance(source_or_rejection, StrictRejection):
        return source_or_rejection

    return StrictPlugin(
        name=name,
        source=source_or_rejection,
        description=description,
        version=version,
        tags=tags,
    )


def _resolve_repository_shape(
    *,
    name: str,
    entry: dict[str, Any],
    upstream_host: str,
) -> StrictPluginSource | StrictRejection:
    """Resolve the Copilot-CLI ``repository: owner/repo`` shape."""
    repo = entry.get("repository")
    if not isinstance(repo, str) or not _REMOTE_SOURCE_RE.match(repo):
        return StrictRejection(
            plugin_name=name,
            reason="invalid-repo",
            detail=f"plugin '{name}' has invalid 'repository': {repo!r}",
        )

    ref_value = entry.get("ref")
    sha_value = entry.get("sha")
    ref = _validate_ref(name, ref_value)
    if isinstance(ref, StrictRejection):
        return ref
    sha = _validate_sha(name, sha_value)
    if isinstance(sha, StrictRejection):
        return sha

    return StrictPluginSource(
        type="github",
        host=upstream_host,
        repo=repo,
        ref=ref,
        sha=sha,
    )


def _resolve_source_field(
    *,
    name: str,
    raw: Any,
    entry: dict[str, Any],
    upstream_owner_repo: str,
    upstream_host: str,
    plugin_root: str,
) -> StrictPluginSource | StrictRejection:
    """Resolve the Claude-shape ``source`` field (string or dict)."""
    if isinstance(raw, str):
        return _resolve_string_source(
            name=name,
            raw=raw,
            entry=entry,
            upstream_owner_repo=upstream_owner_repo,
            upstream_host=upstream_host,
            plugin_root=plugin_root,
        )
    if isinstance(raw, dict):
        return _resolve_dict_source(
            name=name,
            raw=raw,
            entry=entry,
            upstream_host=upstream_host,
        )
    return StrictRejection(
        plugin_name=name,
        reason="invalid-source-shape",
        detail=(
            f"plugin '{name}' has 'source' of unsupported type "
            f"{type(raw).__name__}; expected string or object"
        ),
    )


def _resolve_string_source(
    *,
    name: str,
    raw: str,
    entry: dict[str, Any],
    upstream_owner_repo: str,
    upstream_host: str,
    plugin_root: str,
) -> StrictPluginSource | StrictRejection:
    """Resolve a string-shorthand source as a subdir of the upstream repo.

    The shorthand is resolved as a subdirectory of the upstream
    marketplace's repository; ``metadata.pluginRoot`` (when set) acts
    as the containment base. ``..`` segments are rejected outright,
    and the resolved subdir is required to remain under
    ``pluginRoot``.
    """
    cleaned = raw.strip()
    if not cleaned:
        return StrictRejection(
            plugin_name=name,
            reason="invalid-source-shape",
            detail=f"plugin '{name}' has empty 'source' string",
        )

    # Normalise leading ``./`` BEFORE traversal validation so the
    # idiomatic Claude-shorthand form is accepted; ``..`` segments
    # remain rejected.
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]

    if cleaned.startswith("/"):
        return StrictRejection(
            plugin_name=name,
            reason="path-traversal",
            detail=f"plugin '{name}' has absolute 'source' path: {raw!r}",
        )

    try:
        validate_path_segments(cleaned, context=f"plugin '{name}' source")
    except PathTraversalError as exc:
        return StrictRejection(
            plugin_name=name,
            reason="path-traversal",
            detail=str(exc),
        )

    rel = cleaned

    if plugin_root:
        # Compose subdir as ``<pluginRoot>/<rel>`` and verify the
        # resulting parts contain no ``..``. PurePosixPath collapses
        # ``./`` segments deterministically.
        composed = PurePosixPath(plugin_root) / rel
        composed_str = str(composed)
        try:
            validate_path_segments(composed_str, context=f"plugin '{name}' resolved source")
        except PathTraversalError as exc:
            return StrictRejection(
                plugin_name=name,
                reason="path-traversal",
                detail=str(exc),
            )
        # Defence-in-depth: composed path must still start with
        # plugin_root (PurePosixPath should preserve this; we assert
        # to catch any future contributor mistakes).
        composed_parts = composed.parts
        root_parts = PurePosixPath(plugin_root).parts
        if composed_parts[: len(root_parts)] != root_parts:
            return StrictRejection(
                plugin_name=name,
                reason="relative-source-out-of-root",
                detail=(f"plugin '{name}' source {raw!r} escapes pluginRoot '{plugin_root}'"),
            )
        subdir = composed_str
    else:
        subdir = rel

    ref_value = entry.get("ref")
    sha_value = entry.get("sha")
    ref = _validate_ref(name, ref_value)
    if isinstance(ref, StrictRejection):
        return ref
    sha = _validate_sha(name, sha_value)
    if isinstance(sha, StrictRejection):
        return sha

    return StrictPluginSource(
        type="git-subdir",
        host=upstream_host,
        repo=upstream_owner_repo,
        ref=ref,
        sha=sha,
        subdir=subdir or None,
    )


def _resolve_dict_source(
    *,
    name: str,
    raw: dict[str, Any],
    entry: dict[str, Any],
    upstream_host: str,
) -> StrictPluginSource | StrictRejection:
    """Resolve a dict ``source`` field (Claude format)."""
    # Claude Code uses ``type``; the lenient parser also tolerates an
    # alternate ``source`` key inside the dict. Strict parsing requires
    # ``type`` to be set explicitly so that ``unsupported-source-type``
    # rejections name the actual offending value.
    source_type = raw.get("type", "")
    if not isinstance(source_type, str) or not source_type:
        return StrictRejection(
            plugin_name=name,
            reason="invalid-source-shape",
            detail=f"plugin '{name}' source object missing 'type' discriminator",
        )

    if source_type == "npm":
        return StrictRejection(
            plugin_name=name,
            reason="npm-source",
            detail=(
                f"plugin '{name}' uses 'npm' source type which is not "
                f"supported by APM upstreams (v1)"
            ),
        )

    if source_type not in {"github", "git-subdir"}:
        return StrictRejection(
            plugin_name=name,
            reason="unsupported-source-type",
            detail=(
                f"plugin '{name}' uses source.type='{source_type}', "
                f"expected one of {{github, git-subdir}}"
            ),
        )

    allowed_keys = _GITHUB_SOURCE_KEYS if source_type == "github" else _GIT_SUBDIR_SOURCE_KEYS
    unknown = set(raw.keys()) - allowed_keys
    if unknown:
        return StrictRejection(
            plugin_name=name,
            reason="unknown-source-key",
            detail=(
                f"plugin '{name}' source has unknown key(s): {sorted(unknown)}. "
                f"Allowed for type='{source_type}': {sorted(allowed_keys)}"
            ),
        )

    # Host: defaults to upstream host; explicit value must match v1
    # allow-list (no cross-host plugins).
    host = raw.get("host", upstream_host)
    if not isinstance(host, str) or host not in _SUPPORTED_HOSTS:
        return StrictRejection(
            plugin_name=name,
            reason="unsupported-host",
            detail=(
                f"plugin '{name}' source.host='{host}' is not supported in v1 "
                f"(supported: {sorted(_SUPPORTED_HOSTS)})"
            ),
        )

    # ``repo`` may be expressed as ``repo: owner/repo`` or as a full
    # ``url:`` (for git-subdir). Only the former is supported in v1 to
    # avoid URL-shape ambiguity.
    repo = raw.get("repo")
    if "url" in raw and not repo:
        return StrictRejection(
            plugin_name=name,
            reason="invalid-repo",
            detail=(
                f"plugin '{name}' uses source.url; APM upstreams require "
                f"source.repo='owner/repo' in v1"
            ),
        )
    if not isinstance(repo, str) or not _REMOTE_SOURCE_RE.match(repo):
        return StrictRejection(
            plugin_name=name,
            reason="invalid-repo",
            detail=f"plugin '{name}' has invalid source.repo: {repo!r}",
        )

    subdir: str | None = None
    if source_type == "git-subdir":
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            return StrictRejection(
                plugin_name=name,
                reason="missing-subdir",
                detail=f"plugin '{name}' git-subdir source missing 'path'",
            )
        try:
            validate_path_segments(path, context=f"plugin '{name}' source.path")
        except PathTraversalError as exc:
            return StrictRejection(
                plugin_name=name,
                reason="path-traversal",
                detail=str(exc),
            )
        subdir = path.strip().lstrip("./")
        if subdir.startswith("/"):
            return StrictRejection(
                plugin_name=name,
                reason="path-traversal",
                detail=f"plugin '{name}' has absolute source.path: {path!r}",
            )

    ref_value = raw.get("ref") or entry.get("ref")
    sha_value = raw.get("sha") or entry.get("sha")
    ref = _validate_ref(name, ref_value)
    if isinstance(ref, StrictRejection):
        return ref
    sha = _validate_sha(name, sha_value)
    if isinstance(sha, StrictRejection):
        return sha

    return StrictPluginSource(
        type=source_type,
        host=host,
        repo=repo,
        ref=ref,
        sha=sha,
        subdir=subdir,
    )


def _validate_ref(name: str, value: Any) -> str | None | StrictRejection:
    """Validate a ref value or return a rejection.

    Empty / missing -> ``None`` (the resolver applies the precedence
    ladder). Non-string or shape-violating ref -> rejection.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _REF_RE.match(value):
        return StrictRejection(
            plugin_name=name,
            reason="invalid-ref",
            detail=f"plugin '{name}' has invalid ref: {value!r}",
        )
    return value


def _validate_sha(name: str, value: Any) -> str | None | StrictRejection:
    """Validate a 40-char git SHA or return a rejection."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _FULL_SHA_RE.match(value):
        return StrictRejection(
            plugin_name=name,
            reason="invalid-sha",
            detail=(
                f"plugin '{name}' has invalid sha: {value!r}; "
                f"strict parser requires a full 40-char hex SHA"
            ),
        )
    return value
