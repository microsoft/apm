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
from typing import Callable, Optional, Tuple

from ..utils.path_security import PathTraversalError, validate_path_segments
from ..models.dependency.reference import DependencyReference
from .client import fetch_or_cache
from .errors import MarketplaceFetchError, PluginNotFoundError
from .models import MarketplacePlugin
from .registry import get_marketplace_by_name

logger = logging.getLogger(__name__)

_MARKETPLACE_RE = re.compile(
    r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._-]+)(?:#(.+))?$"
)

# Characters that signal a semver range rather than a raw git ref
_SEMVER_RANGE_CHARS = re.compile(r"[~^<>=!]")


def parse_marketplace_ref(
    specifier: str,
) -> Optional[Tuple[str, str, Optional[str]]]:
    """Parse a ``NAME@MARKETPLACE[#ref]`` specifier.

    The optional ``#ref`` suffix carries a raw git ref (tag, branch, or
    SHA).  Semver range characters (``^``, ``~``, ``>=``, ``<``, ``!=``)
    are **rejected** with a ``ValueError`` -- marketplace refs are raw
    git refs, not version constraints.

    Returns:
        ``(plugin_name, marketplace_name, ref_or_none)`` if the
        specifier matches, or ``None`` if it does not look like a
        marketplace ref.

    Raises:
        ValueError: If the ``#`` suffix contains semver range characters.
    """
    s = specifier.strip()
    # Quick rejection: slashes and colons *before* the fragment belong to
    # other formats.  Split on ``#`` first so that refs with slashes
    # (e.g. ``feature/branch``) don't cause a false rejection.
    head = s.split("#", 1)[0]
    if "/" in head or ":" in head:
        return None
    match = _MARKETPLACE_RE.match(s)
    if match:
        ref = match.group(3)
        if ref and _SEMVER_RANGE_CHARS.search(ref):
            raise ValueError(
                "Semver ranges are not supported in marketplace refs. "
                "Use a raw git tag, branch, or SHA instead "
                "(e.g. 'plugin@mkt#v2.0.0'). "
                "See: https://microsoft.github.io/apm/guides/marketplaces/"
            )
        return (match.group(1), match.group(2), ref)
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

    Delegates to ``DependencyReference.parse()`` to extract the
    ``owner/repo`` coordinate from any valid Git URL (GitHub, GHES, GitLab,
    Bitbucket, ADO, SSH).  The URL's host is *not* preserved -- downstream
    resolution (``RefResolver``) uses the configured ``GITHUB_HOST`` for
    ``git ls-remote``.  True cross-host resolution is tracked in #1010.
    """
    url = source.get("url", "")
    if not url:
        raise ValueError("URL source requires a non-empty 'url' field")
    try:
        dep = DependencyReference.parse(url)
    except ValueError as exc:
        raise ValueError(
            f"Cannot resolve URL source '{url}': {exc}"
        ) from exc
    if dep.is_local:
        raise ValueError(
            f"URL source '{url}' resolves to a local path, not a Git coordinate."
        )
    if dep.reference:
        return f"{dep.repo_url}#{dep.reference}"
    return dep.repo_url


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

    Handles 4 source types: relative, github, url, git-subdir.
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
    version_spec: Optional[str] = None,
    auth_resolver: Optional[object] = None,
    warning_handler: Optional[Callable[[str], None]] = None,
) -> Tuple[str, MarketplacePlugin]:
    """Resolve a marketplace plugin reference to a canonical string.

    When *version_spec* is given it is treated as a raw git ref override
    that replaces the plugin's ``source.ref``.  When ``None`` the ref
    from the marketplace entry is used as-is.

    Args:
        plugin_name: Plugin name within the marketplace.
        marketplace_name: Registered marketplace name.
        version_spec: Optional raw git ref override (e.g. ``"v2.0.0"``
            or ``"main"``).  ``None`` uses the marketplace entry's
            ``source.ref``.
        auth_resolver: Optional ``AuthResolver`` instance.
        warning_handler: Optional callback for security warnings.  When
            provided, warnings (immutability violations, shadow detections)
            are forwarded here instead of being emitted through Python
            stdlib logging.  Callers typically pass
            ``CommandLogger.warning`` so warnings render through the CLI
            output system.

    Returns:
        Tuple of (canonical ``owner/repo[#ref]`` string, resolved plugin).

    Raises:
        MarketplaceNotFoundError: If the marketplace is not registered.
        PluginNotFoundError: If the plugin is not in the marketplace.
        MarketplaceFetchError: If the marketplace cannot be fetched.
        ValueError: If the plugin source cannot be resolved.
    """

    def _emit_warning(msg: str) -> None:
        """Route warning through handler when available, else stdlib."""
        if warning_handler is not None:
            warning_handler(msg)
        else:
            logger.warning("%s", msg)

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

    # ---- Raw ref override ----
    # When version_spec is provided it is treated as a raw git ref that
    # overrides whatever ref came from the marketplace source field.
    if version_spec:
        base = canonical.split("#", 1)[0]
        canonical = f"{base}#{version_spec}"
        logger.debug(
            "Using raw git ref '%s' for %s@%s",
            version_spec,
            plugin_name,
            marketplace_name,
        )

    # ---- Ref immutability check (advisory) ----
    # Record the plugin -> ref mapping (scoped by version) and warn if
    # it changed since the last install (potential ref-swap attack).
    # Using the plugin's declared version field ensures legitimate
    # version bumps never trigger false-positive warnings.
    current_ref = canonical.split("#", 1)[1] if "#" in canonical else None
    plugin_version = plugin.version or ""
    if current_ref:
        from .version_pins import check_ref_pin, record_ref_pin

        previous_ref = check_ref_pin(
            marketplace_name, plugin_name, current_ref,
            version=plugin_version,
        )
        if previous_ref is not None:
            _emit_warning(
                "Plugin %s@%s ref changed: was '%s', now '%s'. "
                "This may indicate a ref swap attack."
                % (
                    plugin_name,
                    marketplace_name,
                    previous_ref,
                    current_ref,
                )
            )
        record_ref_pin(
            marketplace_name, plugin_name, current_ref,
            version=plugin_version,
        )

    logger.debug(
        "Resolved %s@%s -> %s",
        plugin_name,
        marketplace_name,
        canonical,
    )

    # -- Shadow detection (advisory) --
    # Warn when the same plugin name exists in other registered
    # marketplaces.  This helps users notice potential name-squatting
    # where an attacker publishes a same-named plugin in a secondary
    # marketplace.
    try:
        from .shadow_detector import detect_shadows

        shadows = detect_shadows(
            plugin_name, marketplace_name, auth_resolver=auth_resolver
        )
        for shadow in shadows:
            _emit_warning(
                "Plugin '%s' also found in marketplace '%s'. "
                "Verify you are installing from the intended source."
                % (plugin_name, shadow.marketplace_name)
            )
    except Exception:
        # Shadow detection must never break installation
        logger.debug("Shadow detection failed", exc_info=True)

    return canonical, plugin
