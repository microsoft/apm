"""Frozen dataclasses and JSON parser for marketplace manifests.

Supports both Copilot CLI and Claude Code marketplace.json formats,
plus the Agent Skills Discovery RFC v0.2.0 index format.
All dataclasses are frozen for thread-safety.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Agent Skills Discovery RFC v0.2.0 -- the only schema version we accept.
_AGENT_SKILLS_SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"

# RFC skill-name rule: 1-64 chars, lowercase alphanumeric + hyphens,
# no leading/trailing/consecutive hyphens.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class MarketplaceSource:
    """A registered marketplace repository.

    Stored in ``~/.apm/marketplaces.json``.

    Two source types are supported:
    - ``"github"`` (default) -- a GitHub-hosted marketplace.json index.
    - ``"url"`` -- an arbitrary HTTPS Agent Skills discovery endpoint.
    """

    name: str  # Display name (e.g., "acme-tools")
    owner: str = ""  # GitHub owner (GitHub sources only)
    repo: str = ""  # GitHub repo  (GitHub sources only)
    host: str = "github.com"  # Git host FQDN (GitHub sources only)
    branch: str = "main"  # Git branch (GitHub sources only)
    path: str = "marketplace.json"  # Detected on add (GitHub sources only)
    source_type: str = "github"  # "github" | "url"
    url: str = ""  # Fully-qualified index URL (URL sources only)

    @property
    def is_url_source(self) -> bool:
        """Return True if this is a URL-based (Agent Skills) source."""
        return self.source_type == "url"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        if self.is_url_source:
            return {
                "name": self.name,
                "source_type": "url",
                "url": self.url,
            }
        # GitHub sources omit source_type so existing consumers parse unchanged
        result: Dict[str, Any] = {
            "name": self.name,
            "owner": self.owner,
            "repo": self.repo,
        }
        if self.host != "github.com":
            result["host"] = self.host
        if self.branch != "main":
            result["branch"] = self.branch
        if self.path != "marketplace.json":
            result["path"] = self.path
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MarketplaceSource":
        """Deserialize from JSON dict."""
        source_type = data.get("source_type", "github")
        if source_type == "url":
            url = data.get("url", "")
            if not url:
                raise ValueError(
                    "URL source requires a non-empty 'url' field"
                )
            return cls(
                name=data["name"],
                source_type="url",
                url=url,
            )
        if source_type != "github":
            raise ValueError(f"Unsupported marketplace source_type: {source_type!r}")
        return cls(
            name=data["name"],
            owner=data.get("owner", ""),
            repo=data.get("repo", ""),
            host=data.get("host", "github.com"),
            branch=data.get("branch", "main"),
            path=data.get("path", "marketplace.json"),
            source_type="github",
        )


@dataclass(frozen=True)
class MarketplacePlugin:
    """A single plugin entry inside a marketplace manifest."""

    name: str  # Plugin name (unique within marketplace)
    source: Any = None  # String (relative) or dict (github/url/git-subdir)
    description: str = ""
    version: str = ""
    tags: Tuple[str, ...] = ()
    source_marketplace: str = ""  # Populated during resolution

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
    plugins: Tuple[MarketplacePlugin, ...] = ()
    owner_name: str = ""
    description: str = ""
    plugin_root: str = ""  # metadata.pluginRoot - base path for bare-name sources
    source_url: str = ""
    source_digest: str = ""

    def find_plugin(self, plugin_name: str) -> Optional[MarketplacePlugin]:
        """Find a plugin by exact name (case-insensitive)."""
        lower = plugin_name.lower()
        for p in self.plugins:
            if p.name.lower() == lower:
                return p
        return None

    def search(self, query: str) -> List[MarketplacePlugin]:
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

def _parse_plugin_entry(
    entry: Dict[str, Any], source_name: str
) -> Optional[MarketplacePlugin]:
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
                logger.debug(
                    "Skipping npm source type for plugin '%s' (unsupported)", name
                )
                return None
            # Normalize: ensure "type" key is set for downstream resolvers
            if source_type and "type" not in raw:
                raw = {**raw, "type": source_type}
            source = raw
        else:
            logger.debug(
                "Skipping plugin '%s' with unrecognized source format", name
            )
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

    return MarketplacePlugin(
        name=name,
        source=source,
        description=description,
        version=version,
        tags=tags,
        source_marketplace=source_name,
    )


def parse_marketplace_json(
    data: Dict[str, Any],
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
        source_url: URL from which the index was fetched (optional, for provenance).
        source_digest: SHA-256 digest of the raw index bytes (optional, for provenance).

    Returns:
        MarketplaceManifest: Parsed manifest with valid plugin entries.
    """
    manifest_name = data.get("name", source_name or "unknown")
    description = data.get("description", "")
    owner_name = data.get("owner", {}).get("name", "") if isinstance(
        data.get("owner"), dict
    ) else data.get("owner", "")

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

    plugins: List[MarketplacePlugin] = []
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


# ---------------------------------------------------------------------------
# Agent Skills Discovery RFC v0.2.0 index parser
# ---------------------------------------------------------------------------

def _is_valid_skill_name(name: str) -> bool:
    """Return True if *name* satisfies the RFC skill-name rules."""
    if not name or len(name) > 64:
        return False
    if not _SKILL_NAME_RE.match(name):
        return False
    if "--" in name:
        return False
    return True


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_valid_digest(digest: str) -> bool:
    """Return True if *digest* matches the ``sha256:{64 hex chars}`` format."""
    return bool(digest and _DIGEST_RE.match(digest))


def parse_agent_skills_index(
    data: Dict[str, Any],
    source_name: str = "",
    *,
    source_url: str = "",
    source_digest: str = "",
) -> "MarketplaceManifest":
    """Parse an Agent Skills Discovery RFC v0.2.0 index into a ``MarketplaceManifest``.

    Args:
        data: Parsed JSON of an ``index.json`` served at
              ``/.well-known/agent-skills/index.json``.
        source_name: Display name of the marketplace (for provenance).
        source_url: URL from which the index was fetched (optional, for provenance).
        source_digest: SHA-256 digest of the raw index bytes (optional, for provenance).

    Returns:
        MarketplaceManifest: Parsed manifest with valid skill entries.

    Raises:
        ValueError: If ``$schema`` is missing or not the known v0.2.0 URI.
            Clients MUST NOT process an index with an unrecognized schema
            (RFC requirement).
    """
    schema = data.get("$schema")
    if not isinstance(schema, str) or schema != _AGENT_SKILLS_SCHEMA:
        raise ValueError(
            f"Unrecognized or missing Agent Skills index $schema: {schema!r}. "
            f"Expected: {_AGENT_SKILLS_SCHEMA!r}"
        )

    raw_skills = data.get("skills", [])
    if not isinstance(raw_skills, list):
        logger.warning("Agent Skills index 'skills' field is not a list in '%s'", source_name)
        raw_skills = []

    plugins: List[MarketplacePlugin] = []
    for entry in raw_skills:
        if not isinstance(entry, dict):
            logger.debug(
                "Skipping non-dict entry in Agent Skills array in '%s'", source_name
            )
            continue
        name = entry.get("name", "")
        if not isinstance(name, str):
            logger.debug(
                "Skipping Agent Skills entry with non-string name %r in '%s'",
                name,
                source_name,
            )
            continue
        name = name.strip()
        if not name:
            logger.debug(
                "Skipping Agent Skills entry with empty name in '%s'", source_name
            )
            continue
        if not _is_valid_skill_name(name):
            logger.warning(
                "Skipping Agent Skills entry with invalid name %r in '%s' "
                "(name must be 1-64 lowercase alphanumeric/hyphen characters)",
                name,
                source_name,
            )
            continue
        skill_type = entry.get("type", "")
        if not isinstance(skill_type, str) or skill_type not in ("skill-md", "archive"):
            logger.warning(
                "Skipping Agent Skills entry %r with unsupported type %r in '%s'",
                name,
                skill_type,
                source_name,
            )
            continue
        url = entry.get("url", "")
        if not isinstance(url, str) or not url:
            logger.warning(
                "Skipping Agent Skills entry %r with missing/invalid url in '%s'",
                name,
                source_name,
            )
            continue
        digest = entry.get("digest", "")
        if not _is_valid_digest(digest):
            logger.debug(
                "Skipping Agent Skills entry %r with invalid digest %r in '%s'",
                name,
                digest,
                source_name,
            )
            continue
        description = entry.get("description", "")
        if not isinstance(description, str):
            description = ""
        plugins.append(
            MarketplacePlugin(
                name=name,
                source={"type": skill_type, "url": url, "digest": digest},
                description=description,
                source_marketplace=source_name,
            )
        )

    return MarketplaceManifest(
        name=source_name or "unknown",
        plugins=tuple(plugins),
        source_url=source_url,
        source_digest=source_digest,
    )
