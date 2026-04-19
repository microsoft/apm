"""Resolve ``NAME@MARKETPLACE`` specifiers to canonical ``owner/repo#ref`` strings.

The ``@`` disambiguation rule:
- If input matches ``^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$`` (no ``/``, no ``:``),
  it is a marketplace ref.
- Everything else goes to the existing ``DependencyReference.parse()`` path.
- These inputs previously raised ``ValueError`` ("Use 'user/repo' format"),
  so this is a backward-compatible grammar extension.
"""

import logging
import re
from typing import Optional, Tuple

from ..utils.path_security import PathTraversalError, validate_path_segments
from .client import fetch_or_cache
from .errors import MarketplaceFetchError, PluginNotFoundError
from .models import MarketplacePlugin
from .registry import get_marketplace_by_name

logger = logging.getLogger(__name__)

_MARKETPLACE_RE = re.compile(r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._-]+)$")


def parse_marketplace_ref(specifier: str) -> Optional[Tuple[str, str]]:
    """Parse a ``NAME@MARKETPLACE`` specifier.

    Returns:
        ``(plugin_name, marketplace_name)`` if the specifier matches,
        or ``None`` if it does not look like a marketplace ref.
    """
    s = specifier.strip()
    # Quick rejection: slashes and colons belong to other formats
    if "/" in s or ":" in s:
        return None
    match = _MARKETPLACE_RE.match(s)
    if match:
        return (match.group(1), match.group(2))
    return None


def _resolve_github_source(source: dict) -> str:
    """Resolve a ``github`` source type to ``owner/repo[/path][#ref]``.

    Accepts ``path`` field (Copilot CLI format) as a virtual subdirectory.
    """
    repo = source.get("repo", "")
    ref = source.get("ref", "")
    path = source.get("path", "").strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid github source: 'repo' field must be 'owner/repo', got '{repo}'"
        )
    if path:
        try:
            validate_path_segments(path, context="github source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        base = f"{repo}/{path}"
    else:
        base = repo
    if ref:
        return f"{base}#{ref}"
    return base


def _resolve_url_source(source: dict) -> str:
    """Resolve a ``url`` source type.

    GitHub repo URLs are resolved to ``owner/repo``.
    Any other HTTPS URL is returned as-is so that Agent Skills CDN entries
    and arbitrary HTTPS plugin sources are passed through to the installer.
    """
    url = source.get("url", "")
    if not url:
        raise ValueError("url source has an empty 'url' field")

    # Try to extract owner/repo from common GitHub URL patterns
    for prefix in ("https://github.com/", "http://github.com/"):
        if url.lower().startswith(prefix):
            path = url[len(prefix) :].rstrip("/").split("?")[0]
            # Remove .git suffix
            if path.endswith(".git"):
                path = path[:-4]
            parts = path.split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"

    # Non-GitHub HTTPS URL -- return as-is (CDN, arbitrary HTTPS host, etc.)
    from urllib.parse import urlparse as _urlparse

    if _urlparse(url).scheme.lower() == "https":
        return url

    raise ValueError(
        f"Cannot resolve URL source '{url}' to a Git coordinate. "
        f"APM requires Git-based sources (owner/repo format) or HTTPS URLs."
    )


def _resolve_git_subdir_source(source: dict) -> str:
    """Resolve a ``git-subdir`` source type to ``owner/repo[/subdir][#ref]``."""
    repo = source.get("repo", "")
    ref = source.get("ref", "")
    subdir = (source.get("subdir", "") or source.get("path", "")).strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid git-subdir source: 'repo' must be 'owner/repo', got '{repo}'"
        )
    if subdir:
        try:
            validate_path_segments(subdir, context="git-subdir source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        base = f"{repo}/{subdir}"
    else:
        base = repo
    if ref:
        return f"{base}#{ref}"
    return base


def _resolve_relative_source(
    source: str,
    marketplace_owner: str,
    marketplace_repo: str,
    plugin_root: str = "",
) -> str:
    """Resolve a relative path source to ``owner/repo[/subdir]``.

    Relative sources point to subdirectories within the marketplace repo itself.
    When *plugin_root* is set (from ``metadata.pluginRoot`` in the manifest),
    bare names (no ``/``) are resolved under that directory.
    """
    # Normalize the relative path (strip leading ./ and trailing /)
    rel = source.strip("/")
    if rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")

    # If plugin_root is set and source is a bare name, prepend it
    if plugin_root and rel and rel != "." and "/" not in rel:
        root = plugin_root.strip("/")
        if root.startswith("./"):
            root = root[2:]
        root = root.strip("/")
        if root:
            rel = f"{root}/{rel}"

    if rel and rel != ".":
        try:
            validate_path_segments(rel, context="relative source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        return f"{marketplace_owner}/{marketplace_repo}/{rel}"
    return f"{marketplace_owner}/{marketplace_repo}"


def resolve_plugin_source(
    plugin: MarketplacePlugin,
    marketplace_owner: str = "",
    marketplace_repo: str = "",
    plugin_root: str = "",
) -> str:
    """Resolve a plugin's source to a canonical ``owner/repo[#ref]`` string.

    Handles 6 source types: relative, github, url, skill-md, archive, git-subdir.
    NPM sources are rejected with a clear message.

    Args:
        plugin: The marketplace plugin to resolve.
        marketplace_owner: Owner of the marketplace repo (for relative sources).
        marketplace_repo: Repo name of the marketplace (for relative sources).
        plugin_root: Base path for bare-name sources (from metadata.pluginRoot).

    Returns:
        Canonical ``owner/repo[#ref]`` string.

    Raises:
        ValueError: If the source type is unsupported or the source is invalid.
    """
    source = plugin.source
    if source is None:
        raise ValueError(f"Plugin '{plugin.name}' has no source defined")

    # String source = relative path
    if isinstance(source, str):
        if not marketplace_owner:
            raise ValueError(
                f"Plugin '{plugin.name}' has a relative source but no "
                "marketplace_owner is available"
            )
        return _resolve_relative_source(
            source, marketplace_owner, marketplace_repo, plugin_root=plugin_root
        )

    if not isinstance(source, dict):
        raise ValueError(
            f"Plugin '{plugin.name}' has unrecognized source format: {type(source).__name__}"
        )

    source_type = source.get("type", "")

    if source_type == "github":
        return _resolve_github_source(source)
    elif source_type in ("skill-md", "archive"):
        # Agent Skills RFC types -- the canonical reference is the download URL
        url = source.get("url", "")
        if not url:
            raise ValueError(
                f"Plugin '{plugin.name}' has a '{source_type}' source with no 'url' field"
            )
        return url
    elif source_type == "url":
        return _resolve_url_source(source)
    elif source_type == "git-subdir":
        return _resolve_git_subdir_source(source)
    elif source_type == "npm":
        raise ValueError(
            f"Plugin '{plugin.name}' uses npm source type which is not supported by APM. "
            f"APM requires Git-based sources. "
            f"Consider asking the marketplace maintainer to add a 'github' source."
        )
    else:
        raise ValueError(
            f"Plugin '{plugin.name}' has unsupported source type: '{source_type}'"
        )


def resolve_marketplace_plugin(
    plugin_name: str,
    marketplace_name: str,
    *,
    auth_resolver: Optional[object] = None,
) -> Tuple[str, MarketplacePlugin]:
    """Resolve a marketplace plugin reference to a canonical string.

    Args:
        plugin_name: Plugin name within the marketplace.
        marketplace_name: Registered marketplace name.
        auth_resolver: Optional ``AuthResolver`` instance.

    Returns:
        Tuple of (canonical ``owner/repo[#ref]`` string, resolved plugin).

    Raises:
        MarketplaceNotFoundError: If the marketplace is not registered.
        PluginNotFoundError: If the plugin is not in the marketplace.
        MarketplaceFetchError: If the marketplace cannot be fetched.
        ValueError: If the plugin source cannot be resolved.
    """
    source = get_marketplace_by_name(marketplace_name)
    manifest = fetch_or_cache(source, auth_resolver=auth_resolver)

    plugin = manifest.find_plugin(plugin_name)
    if plugin is None:
        raise PluginNotFoundError(plugin_name, marketplace_name)

    canonical = resolve_plugin_source(
        plugin,
        marketplace_owner=source.owner,
        marketplace_repo=source.repo,
        plugin_root=manifest.plugin_root,
    )

    logger.debug(
        "Resolved %s@%s -> %s",
        plugin_name,
        marketplace_name,
        canonical,
    )

    return canonical, plugin
