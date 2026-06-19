"""Frozen dataclasses and JSON parser for marketplace manifests.

Supports both Copilot CLI and Claude Code marketplace.json formats.
All dataclasses are frozen for thread-safety.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from apm_cli.cache.url_normalize import SCP_LIKE_RE as _SCP_LIKE_RE

logger = logging.getLogger(__name__)


# SCP-like git URL: e.g. git@host:org/repo.git -- reused canonical regex
# from apm_cli.cache.url_normalize so dependency and marketplace SCP parsing
# never drift.


def _looks_like_local_path(value: str) -> bool:
    """Heuristic for local filesystem paths and file:// URIs."""
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if value.startswith(("/", "./", "../", "~")):
        return True
    # Windows drive letter: C:\ or C:/
    return bool(len(value) >= 3 and value[1:3] in (":\\", ":/") and value[0].isalpha())


def _extract_host_from_url(url: str) -> str:
    """Best-effort host extraction from any URL/path; empty for local paths."""
    if not url or _looks_like_local_path(url):
        return ""
    scp = _SCP_LIKE_RE.match(url)
    if scp:
        return scp.group("host")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return parsed.hostname or ""


def url_names_remote_manifest(url: str) -> bool:
    """Return True when *url* is a direct hosted ``marketplace.json`` document.

    Single source of truth for the "is this a hosted marketplace.json URL"
    decision, shared by the marketplace CLI (``_is_remote_marketplace_json_url``)
    and ``MarketplaceSource.is_remote_manifest_url`` so the two predicates
    cannot drift (see #692 forward-compat constraint 1: the source-kind
    discriminator stays a derived, validated string). Matches Anthropic's
    hosted ``marketplace.json`` shape: HTTPS scheme, a host, and a path that
    ends in ``/marketplace.json``.
    """
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    return (parsed.path or "").rstrip("/").endswith("/marketplace.json")


def _extract_owner_repo_from_url(url: str) -> tuple[str, str]:
    """Best-effort owner/repo extraction. Empty strings if not derivable."""
    if not url or _looks_like_local_path(url):
        return ("", "")
    scp = _SCP_LIKE_RE.match(url)
    if scp:
        path = scp.group("path")
    else:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ("", "")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2:
        # Multi-segment paths (GHES nested groups, GitLab subgroups) are
        # preserved by joining all segments except the last into `owner`.
        return ("/".join(segments[:-1]), segments[-1])
    if len(segments) == 1:
        return ("", segments[0])
    return ("", "")


def _local_path_from_source(value: str) -> str:
    """Normalise a local-path source to an absolute path string.

    Handles three ``file://`` shapes so Windows callers get the right answer:
    - POSIX: ``file:///abs/path`` -> ``/abs/path``
    - Windows proper: ``file:///C:/path`` -> ``C:/path``
    - Windows malformed (e.g. ``f"file://{Path}"`` where Path is ``C:\\...``):
      ``file://C:\\path`` -> ``C:\\path``
    ``urlsplit`` mis-parses the Windows-shaped forms (treats ``C:`` as a host),
    so do the strip manually after detecting a drive-letter prefix.
    """
    if value.startswith("file://"):
        rest = value[len("file://") :]
        if len(rest) >= 3 and rest[0] == "/" and rest[1].isalpha() and rest[2] == ":":
            return rest[1:]
        if len(rest) >= 2 and rest[0].isalpha() and rest[1] == ":":
            return rest
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        return parsed.path or value
    # Plain path: expand ~ only. Avoid Path(...) round-trip which would
    # rewrite POSIX separators to backslashes on Windows.
    return os.path.expanduser(value)


@dataclass(frozen=True)
class MarketplaceSource:
    """A registered marketplace repository.

    Stored in ``~/.apm/marketplaces.json``. URL-first model: ``url`` + ``ref``
    + ``path`` are the canonical fields. Legacy ``owner`` / ``repo`` / ``host``
    / ``branch`` kwargs are accepted for backward compatibility and are
    synthesised from / into the URL during construction.
    """

    name: str  # Display name (e.g., "acme-tools")
    url: str = ""  # Canonical URL or local path; synthesised from legacy fields if absent
    ref: str = "main"  # Git ref (branch, tag, or SHA)
    path: str = "marketplace.json"  # Path to manifest within the repo
    # Legacy mirror fields -- populated either by the caller or by __post_init__.
    # Retained for backward compat with code that reads source.owner / source.repo /
    # source.host directly, and for one release of dual-emit in to_dict for downgrade.
    owner: str = ""
    repo: str = ""
    host: str = "github.com"
    branch: str = ""  # Legacy alias for ref; if both given, ref wins

    def __post_init__(self) -> None:
        # Reconcile ref / branch: ref is canonical. If only branch was set, copy it onto ref.
        if self.branch and self.branch != "main" and (not self.ref or self.ref == "main"):
            object.__setattr__(self, "ref", self.branch)
        # Always mirror ref onto branch for legacy readers.
        if self.ref:
            object.__setattr__(self, "branch", self.ref)

        # If URL absent but legacy fields are present, synthesise URL.
        if not self.url:
            if self.owner and self.repo:
                host = self.host or "github.com"
                object.__setattr__(self, "url", f"https://{host}/{self.owner}/{self.repo}")
            # If neither URL nor legacy fields are usable, leave url empty; callers/tests
            # that pass only name=... will fail later when something tries to use it.

        # Backfill legacy mirror fields from URL when caller used URL-only signature.
        if self.url and self.path != "" and not self.owner and not self.repo:
            o, r = _extract_owner_repo_from_url(self.url)
            if o:
                object.__setattr__(self, "owner", o)
            if r:
                object.__setattr__(self, "repo", r)
        if self.url and self.path != "" and self.host == "github.com":
            h = _extract_host_from_url(self.url)
            if h:
                object.__setattr__(self, "host", h)

    # -- derived properties --------------------------------------------------

    @property
    def is_remote_manifest_url(self) -> bool:
        """Return True for direct remote marketplace.json URL sources."""
        return self.path == "" and url_names_remote_manifest(self.url)

    @property
    def kind(self) -> str:
        """Derived source kind: ``local`` | ``url`` | ``github`` | ``gitlab`` | ``git``.

        Classification:
        - Local filesystem path or ``file://`` URI -> ``local``
        - Direct remote marketplace.json URL (``path == ""``) -> ``url``
        - Host classified by AuthResolver as github/ghe_cloud/ghes -> ``github``
        - Host classified as gitlab -> ``gitlab``
        - Anything else (ado, generic, ssh to non-classified host) -> ``git``
        """
        if not self.url or _looks_like_local_path(self.url):
            return "local"
        if self.is_remote_manifest_url:
            return "url"
        host = _extract_host_from_url(self.url)
        if not host:
            return "git"
        # Lazy import to keep models.py free of heavy dependencies
        from apm_cli.core.auth import AuthResolver

        host_kind = AuthResolver.classify_host(host).kind
        if host_kind in ("github", "ghe_cloud", "ghes"):
            return "github"
        if host_kind == "gitlab":
            return "gitlab"
        return "git"

    @property
    def local_path(self) -> str:
        """Return the resolved local filesystem path for ``kind == "local"`` sources.

        Returns an empty string for non-local sources.
        """
        if self.kind != "local":
            return ""
        return _local_path_from_source(self.url)

    @property
    def display_source(self) -> str:
        """Compact, kind-aware string for CLI list rendering."""
        k = self.kind
        if k in ("github", "gitlab") and self.owner and self.repo:
            return f"{self.owner}/{self.repo}"
        if k == "url":
            return self.url
        if k == "local":
            lp = self.local_path
            home = os.path.expanduser("~")
            if home and lp.startswith(home):
                return "~" + lp[len(home) :]
            return lp or self.url
        # generic git: strip scheme for compactness
        url = self.url
        for prefix in ("https://", "http://", "git://"):
            if url.startswith(prefix):
                return url[len(prefix) :]
        return url

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON storage.

        Emits the new URL-first shape plus legacy ``owner``/``repo``/``host``/
        ``branch`` mirror fields for one release so downgrades can still read.
        """
        result: dict[str, Any] = {"name": self.name}
        if self.url:
            result["url"] = self.url
        if self.ref and self.ref != "main":
            result["ref"] = self.ref
        if self.path != "marketplace.json":
            result["path"] = self.path
        # Legacy mirror -- only when meaningful (suppress empty owner/repo for local sources)
        if self.owner:
            result["owner"] = self.owner
        if self.repo:
            result["repo"] = self.repo
        if self.host and self.host != "github.com":
            result["host"] = self.host
        if self.branch and self.branch != "main":
            result["branch"] = self.branch
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketplaceSource:
        """Deserialize from JSON dict.

        Accepts both the new URL-first shape and the legacy
        ``{name, owner, repo, host?, branch?, path?}`` shape. Legacy entries
        are upgraded transparently -- the URL is synthesised from the legacy
        fields and ``ref`` mirrors ``branch``.
        """
        url = data.get("url", "")
        ref = data.get("ref") or data.get("branch") or "main"
        return cls(
            name=data["name"],
            url=url,
            ref=ref,
            path=data.get("path", "marketplace.json"),
            owner=data.get("owner", ""),
            repo=data.get("repo", ""),
            host=data.get("host", "github.com"),
            branch=data.get("branch", ""),
        )


@dataclass(frozen=True)
class MarketplacePlugin:
    """A single plugin entry inside a marketplace manifest."""

    name: str  # Plugin name (unique within marketplace)
    source: Any = None  # String (relative) or dict (github/url/git-subdir/gitlab)
    description: str = ""
    version: str = ""
    tags: tuple[str, ...] = ()
    source_marketplace: str = ""  # Populated during resolution

    # Dedicated registry routing per docs/proposals/registry-api.md §4.5.
    # When set, the plugin resolves through the named APM registry (rather
    # than the existing Git resolver). ``version`` is interpreted as a
    # semver range. ``registry == ""`` (default) keeps existing semantics:
    # the plugin resolves via Git and ``version`` is a Git ref/tag.
    # The discriminator is a separate field (not piggybacking on ``source``)
    # to avoid colliding with the existing source-location semantics where
    # ``source`` is a string path or ``{type: github, repo: ...}`` dict.
    registry: str = ""

    def matches_query(self, query: str) -> bool:
        """Return True if the plugin matches a search query (case-insensitive)."""
        q = query.lower()
        return (
            q in self.name.lower()
            or q in self.description.lower()
            or any(q in tag.lower() for tag in self.tags)
        )


@dataclass(frozen=True)
class MarketplaceManifest:
    """Parsed marketplace.json content."""

    name: str
    plugins: tuple[MarketplacePlugin, ...] = ()
    owner_name: str = ""
    description: str = ""
    plugin_root: str = ""  # metadata.pluginRoot - base path for bare-name sources
    source_url: str = ""
    source_digest: str = ""

    def find_plugin(self, plugin_name: str) -> MarketplacePlugin | None:
        """Find a plugin by exact name (case-insensitive)."""
        lower = plugin_name.lower()
        for p in self.plugins:
            if p.name.lower() == lower:
                return p
        return None

    def search(self, query: str) -> list[MarketplacePlugin]:
        """Search plugins matching a query."""
        return [p for p in self.plugins if p.matches_query(query)]


# ---------------------------------------------------------------------------
# JSON parser -- handles Copilot CLI and Claude Code marketplace.json formats
# ---------------------------------------------------------------------------

# Copilot CLI format:
#   { "name": "...", "plugins": [ { "name": "...", "repository": "owner/repo" } ] }
#
# Claude Code format:
#   { "name": "...", "plugins": [ { "name": "...", "source": { "type": "github", ... } } ] }


def _parse_plugin_entry(entry: dict[str, Any], source_name: str) -> MarketplacePlugin | None:
    """Parse a single plugin entry from either format."""
    name = entry.get("name", "").strip()
    if not name:
        logger.debug("Skipping marketplace plugin entry without a name")
        return None

    description = entry.get("description", "")
    version = entry.get("version", "")
    raw_tags = entry.get("tags", [])
    tags = tuple(raw_tags) if isinstance(raw_tags, list) else ()

    # Determine source -- Copilot uses "repository", Claude uses "source"
    source: Any = None

    if "source" in entry:
        raw = entry["source"]
        if isinstance(raw, str):
            # Relative path source (Claude shorthand)
            source = raw
        elif isinstance(raw, dict):
            # Type discriminator: Copilot CLI uses "source" key, Claude uses "type"
            source_type = raw.get("type", "") or raw.get("source", "")
            if source_type == "npm":
                logger.debug("Skipping npm source type for plugin '%s' (unsupported)", name)
                return None
            # Normalize: ensure "type" key is set for downstream resolvers
            if source_type and "type" not in raw:
                raw = {**raw, "type": source_type}
            source = raw
        else:
            logger.debug("Skipping plugin '%s' with unrecognized source format", name)
            return None
    elif "repository" in entry:
        # Copilot CLI format: "repository": "owner/repo"
        repo = entry["repository"]
        ref = entry.get("ref", "")
        if isinstance(repo, str) and "/" in repo:
            source = {"type": "github", "repo": repo}
            if ref:
                source["ref"] = ref
        else:
            logger.debug(
                "Skipping plugin '%s' with invalid repository field: %s",
                name,
                repo,
            )
            return None
    else:
        logger.debug("Plugin '%s' has no source or repository field", name)
        return None

    # Optional dedicated-registry routing (design §4.5). When ``registry``
    # is set, ``version`` is interpreted as a semver range and the plugin
    # resolves via the dedicated-registry resolver. The marketplace.json
    # parser is intentionally permissive — invalid values are downgraded
    # to "no registry routing" and a debug log line, so a malformed entry
    # doesn't break a whole marketplace fetch.
    registry_name = ""
    raw_registry = entry.get("registry")
    if raw_registry is not None:
        if isinstance(raw_registry, str) and raw_registry.strip():
            registry_name = raw_registry.strip()
        else:
            logger.debug(
                "Plugin '%s' has invalid 'registry' field (expected non-empty string), ignoring",
                name,
            )

    if registry_name and version:
        # Validate semver range for registry-routed plugins. A bad ref
        # surfaces here as a debug log + downgrade to "no registry"
        # rather than a hard fail, since one malformed plugin shouldn't
        # poison a whole marketplace.
        from apm_cli.deps.registry.semver import is_semver_range

        if not is_semver_range(version):
            logger.debug(
                "Plugin '%s' has registry='%s' but version '%s' is not a "
                "semver range; ignoring registry routing for this entry",
                name,
                registry_name,
                version,
            )
            registry_name = ""

    return MarketplacePlugin(
        name=name,
        source=source,
        description=description,
        version=version,
        tags=tags,
        source_marketplace=source_name,
        registry=registry_name,
    )


def parse_marketplace_json(
    data: dict[str, Any],
    source_name: str = "",
    *,
    source_url: str = "",
    source_digest: str = "",
) -> MarketplaceManifest:
    """Parse a marketplace.json dict into a ``MarketplaceManifest``.

    Accepts both Copilot CLI and Claude Code marketplace formats.
    Invalid or unsupported entries are silently skipped with debug logging.

    Args:
        data: Parsed JSON content of marketplace.json.
        source_name: Display name of the marketplace (for provenance).
        source_url: Canonical marketplace source URL for lockfile provenance.
        source_digest: SHA-256 digest of the fetched marketplace.json bytes.

    Returns:
        MarketplaceManifest: Parsed manifest with valid plugin entries.
    """
    manifest_name = data.get("name", source_name or "unknown")
    description = data.get("description", "")
    owner_name = (
        data.get("owner", {}).get("name", "")
        if isinstance(data.get("owner"), dict)
        else data.get("owner", "")
    )

    # Extract pluginRoot from metadata (base path for bare-name sources)
    metadata = data.get("metadata", {})
    plugin_root = ""
    if isinstance(metadata, dict):
        raw_root = metadata.get("pluginRoot", "")
        if isinstance(raw_root, str):
            plugin_root = raw_root.strip()

    raw_plugins = data.get("plugins", [])
    if not isinstance(raw_plugins, list):
        logger.warning(
            "marketplace.json 'plugins' field is not a list in '%s'",
            source_name,
        )
        raw_plugins = []

    plugins: list[MarketplacePlugin] = []
    for entry in raw_plugins:
        if not isinstance(entry, dict):
            continue
        plugin = _parse_plugin_entry(entry, source_name)
        if plugin is not None:
            plugins.append(plugin)

    return MarketplaceManifest(
        name=manifest_name,
        plugins=tuple(plugins),
        owner_name=owner_name,
        description=description,
        plugin_root=plugin_root,
        source_url=source_url,
        source_digest=source_digest,
    )
