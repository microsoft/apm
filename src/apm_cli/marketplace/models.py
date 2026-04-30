"""Frozen dataclasses and JSON parser for marketplace manifests.

Supports both Copilot CLI and Claude Code marketplace.json formats.
All dataclasses are frozen for thread-safety.
"""

import logging
from dataclasses import dataclass, field  # noqa: F401
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401, UP035

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketplaceSource:
    """A registered marketplace repository.

    Stored in ``~/.apm/marketplaces.json``.
    """

    name: str  # Display name (e.g., "acme-tools")
    owner: str  # GitHub owner
    repo: str  # GitHub repo
    host: str = "github.com"
    branch: str = "main"
    path: str = "marketplace.json"  # Detected on add

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON storage."""
        result: dict[str, Any] = {
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
    def from_dict(cls, data: dict[str, Any]) -> "MarketplaceSource":
        """Deserialize from JSON dict."""
        return cls(
            name=data["name"],
            owner=data["owner"],
            repo=data["repo"],
            host=data.get("host", "github.com"),
            branch=data.get("branch", "main"),
            path=data.get("path", "marketplace.json"),
        )


@dataclass(frozen=True)
class MarketplacePlugin:
    """A single plugin entry inside a marketplace manifest."""

    name: str  # Plugin name (unique within marketplace)
    source: Any = None  # String (relative) or dict (github/url/git-subdir)
    description: str = ""
    version: str = ""
    tags: tuple[str, ...] = ()
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
    plugins: tuple[MarketplacePlugin, ...] = ()
    owner_name: str = ""
    description: str = ""
    plugin_root: str = ""  # metadata.pluginRoot - base path for bare-name sources

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

    return MarketplacePlugin(
        name=name,
        source=source,
        description=description,
        version=version,
        tags=tags,
        source_marketplace=source_name,
    )


def parse_marketplace_json(data: dict[str, Any], source_name: str = "") -> MarketplaceManifest:
    """Parse a marketplace.json dict into a ``MarketplaceManifest``.

    Accepts both Copilot CLI and Claude Code marketplace formats.
    Invalid or unsupported entries are silently skipped with debug logging.

    Args:
        data: Parsed JSON content of marketplace.json.
        source_name: Display name of the marketplace (for provenance).

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
    )
