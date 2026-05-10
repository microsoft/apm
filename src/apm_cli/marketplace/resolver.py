"""Resolve ``NAME@MARKETPLACE`` specifiers to canonical ``owner/repo#ref`` strings.

The ``@`` disambiguation rule:
- If input matches ``^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$`` (no ``/``, no ``:``),
  it is a marketplace ref.
- Everything else goes to the existing ``DependencyReference.parse()`` path.
- These inputs previously raised ``ValueError`` ("Use 'user/repo' format"),
  so this is a backward-compatible grammar extension.

For marketplaces on hosts where FQDN shorthand cannot split nested paths safely
(``gitlab.com``, self-managed GitLab **even when not** listed in ``GITLAB_HOST``,
and other non-GitHub / non-ADO FQDNs such as ``git.example.com``), in-marketplace
plugin sources under a subdirectory of the marketplace repository are resolved to a
:class:`~apm_cli.models.dependency.reference.DependencyReference` built like explicit
``git:`` + ``path:``; clone target
is only the registered marketplace project; the plugin directory is ``virtual_path``.
``github.com`` / ``*.ghe.com`` keep shorthand (no structured ref). :func:`resolve_marketplace_plugin` returns
:class:`MarketplacePluginResolution`, which iterates as ``(canonical, plugin)`` so
existing ``canonical, plugin = resolve_marketplace_plugin(...)`` call sites keep
working; consumers that need the structured ref use ``result.dependency_reference``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from ..models.dependency.reference import DependencyReference
from ..utils.github_host import is_azure_devops_hostname, is_github_hostname
from ..utils.path_security import PathTraversalError, validate_path_segments
from .client import fetch_or_cache
from .errors import PluginNotFoundError
from .models import MarketplacePlugin, MarketplaceSource
from .registry import get_marketplace_by_name

logger = logging.getLogger(__name__)

_MARKETPLACE_RE = re.compile(r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._-]+)(?:#(.+))?$")

# Characters that signal a semver range rather than a raw git ref
_SEMVER_RANGE_CHARS = re.compile(r"[~^<>=!]")


@dataclass
class MarketplacePluginResolution:
    """Outcome of :func:`resolve_marketplace_plugin`.

    Iteration yields ``(canonical, plugin)`` so callers can write
    ``canonical, plugin = resolve_marketplace_plugin(...)`` unchanged.
    When :attr:`dependency_reference` is set (GitLab-class in-marketplace
    subdirectory plugins), install logic should prefer it over
    :meth:`~apm_cli.models.dependency.reference.DependencyReference.parse`
    on :attr:`canonical` to avoid mis-parsing nested paths as GitLab project segments.
    """

    canonical: str
    plugin: MarketplacePlugin
    dependency_reference: DependencyReference | None = None

    def __iter__(self) -> Iterator[str | MarketplacePlugin]:
        yield self.canonical
        yield self.plugin


def _normalize_owner_repo_slug(repo: str) -> str:
    """Lowercase ``owner/repo`` slug with optional ``.git`` suffix stripped."""
    r = repo.strip().rstrip("/").lower()
    if r.endswith(".git"):
        r = r[:-4]
    return r


def _marketplace_project_slug(owner: str, repo: str) -> str:
    return _normalize_owner_repo_slug(f"{owner}/{repo}")


def _normalize_repo_field_for_match(repo_field: str, marketplace_host: str) -> str:
    """Normalize a repo field to a logical project path for matching.

    Accept bare ``owner/repo`` paths, host-qualified shorthand like
    ``git.epam.com/owner/repo``, and URL / SSH forms. If the field explicitly names
    a different host than the marketplace host, return an empty string so it does
    not match by suffix alone.
    """
    raw = repo_field.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]

    host_l = marketplace_host.strip().lower()

    if raw.startswith(("http://", "https://", "ssh://")):
        parsed = urlparse(raw)
        parsed_host = (parsed.hostname or "").strip().lower()
        if parsed_host and parsed_host != host_l:
            return ""
        return parsed.path.lstrip("/").lower()

    if raw.startswith("git@") and ":" in raw:
        host_part, path_part = raw[4:].split(":", 1)
        if host_part.strip().lower() != host_l:
            return ""
        return path_part.lstrip("/").lower()

    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 3 and parts[0].strip().lower() == host_l:
        parts = parts[1:]
    return "/".join(parts).lower()


def _repo_field_matches_marketplace(
    repo_field: str, owner: str, repo: str, marketplace_host: str
) -> bool:
    """True if dict ``repo`` identifies the same project as the marketplace source."""
    if not repo_field or "/" not in repo_field:
        return False
    normalized_repo = _normalize_repo_field_for_match(repo_field, marketplace_host)
    if not normalized_repo:
        return False
    return normalized_repo == _marketplace_project_slug(owner, repo)


def _coerce_dict_plugin_type(s: dict) -> str:
    """Return normalized source ``type`` for a plugin entry dict (``type`` / ``source`` / ``kind``).

    ``type`` is case-insensitive. When it is missing, infers ``github`` or
    ``git-subdir`` from ``repo`` plus path fields so in-marketplace matching and
    ``path``/``subdir`` extraction match manifests that only set ``kind`` or omit
    ``type`` (still require a valid ``repo`` for dict sources).
    """
    for key in ("type", "source", "kind"):
        v = s.get(key, "")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    repo = s.get("repo", "")
    if not isinstance(repo, str) or "/" not in repo.strip():
        return ""
    subdir = s.get("subdir", "")
    if isinstance(subdir, str) and subdir.strip():
        return "git-subdir"
    path = s.get("path", "")
    if isinstance(path, str) and path.strip():
        return "github"
    return "github"


def _is_in_marketplace_source(plugin: MarketplacePlugin, source: MarketplaceSource) -> bool:
    """Per spec §Interface Contract — in-marketplace detection."""
    s = plugin.source
    if s is None:
        return False
    if isinstance(s, str):
        return True
    if not isinstance(s, dict):
        return False
    source_type = _coerce_dict_plugin_type(s)
    if source_type in ("github", "git-subdir", "gitlab"):
        return _repo_field_matches_marketplace(
            s.get("repo", ""), source.owner, source.repo, source.host
        )
    return False


def _marketplace_host_needs_explicit_git_path(host: str) -> bool:
    """True when in-repo marketplace plugins must use ``git`` + ``path`` (clone root + subdir).

    ``github.com`` and ``*.ghe.com`` virtual shorthand is reliable. Azure DevOps uses
    a different URL shape and is excluded. Self-managed GitLab FQDNs are often
    classified as ``generic`` by :meth:`AuthResolver.classify_host` when not listed in
    ``GITLAB_HOST`` / ``APM_GITLAB_HOSTS`` — they still need explicit clone URLs so
    paths like ``registry/pkg`` are not treated as extra project namespace segments.
    """
    if not host or not str(host).strip():
        return False
    h = str(host).strip().split("/", 1)[0]
    if is_azure_devops_hostname(h):
        return False
    return not is_github_hostname(h)


def _marketplace_https_git_url(source: MarketplaceSource) -> str:
    """HTTPS clone URL for the registered marketplace project (same project as ``marketplace.json``)."""
    segments = [p for p in f"{source.owner}/{source.repo}".split("/") if p]
    encoded = "/".join(quote(seg, safe="") for seg in segments)
    return f"https://{source.host}/{encoded}.git"


def _extract_in_repo_path_and_ref(
    plugin: MarketplacePlugin, plugin_root: str = ""
) -> tuple[str | None, str | None]:
    """Return ``(in_repo_path, ref)`` for GitLab explicit git+path resolution.

    ``in_repo_path`` is ``None`` when the plugin is the repository root (no
    subdirectory package). ``ref`` is only set for dict sources that declare it.
    """
    src = plugin.source
    if src is None:
        return None, None

    if isinstance(src, str):
        rel = src.strip("/")
        if rel.startswith("./"):
            rel = rel[2:]
        rel = rel.strip("/")

        if plugin_root and rel and rel != "." and "/" not in rel:
            root = plugin_root.strip("/")
            if root.startswith("./"):
                root = root[2:]
            root = root.strip("/")
            if root:
                rel = f"{root}/{rel}"

        if not rel or rel == ".":
            return None, None
        validate_path_segments(rel, context="relative source path")
        return rel, None

    if not isinstance(src, dict):
        return None, None

    source_type = _coerce_dict_plugin_type(src)
    ref_val = src.get("ref", "")
    ref: str | None = ref_val.strip() if isinstance(ref_val, str) and ref_val.strip() else None

    if source_type == "github":
        path = src.get("path", "")
        path = path.strip("/") if isinstance(path, str) else ""
        if not path:
            return None, ref
        validate_path_segments(path, context="github source path")
        return path, ref

    if source_type in ("git-subdir", "gitlab"):
        sub = (src.get("subdir", "") or src.get("path", "")) or ""
        sub = sub.strip("/") if isinstance(sub, str) else ""
        if not sub:
            return None, ref
        validate_path_segments(sub, context="git-subdir source path")
        return sub, ref

    return None, None


def _gitlab_in_marketplace_dependency_reference(
    source: MarketplaceSource,
    in_repo_path: str,
    ref: str | None,
) -> DependencyReference:
    """Build ``DependencyReference`` equivalent to object-form ``git`` + ``path`` (spec)."""
    entry: dict = {"git": _marketplace_https_git_url(source), "path": in_repo_path}
    if ref:
        entry["ref"] = ref
    return DependencyReference.parse_from_dict(entry)


def parse_marketplace_ref(
    specifier: str,
) -> tuple[str, str, str | None] | None:
    """Parse a ``NAME@MARKETPLACE[#ref]`` specifier.

    The optional ``#ref`` suffix carries a raw git ref (tag, branch, or
    SHA). Semver range characters (``^``, ``~``, ``>=``, ``<``, ``!=``)
    are rejected with a ``ValueError`` because marketplace refs are raw
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
    repo = source.get("repo", "") or source.get("repository", "")
    ref = source.get("ref", "")
    path = source.get("path", "").strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid github source: 'repo' (or 'repository') field must be 'owner/repo', got '{repo}'"
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
        raise ValueError(f"Cannot resolve URL source '{url}': {exc}") from exc
    if dep.is_local:
        raise ValueError(f"URL source '{url}' resolves to a local path, not a Git coordinate.")
    if dep.reference:
        return f"{dep.repo_url}#{dep.reference}"
    return dep.repo_url


def _resolve_git_subdir_source(source: dict) -> str:
    """Resolve a ``git-subdir`` source type to ``owner/repo[/subdir][#ref]``."""
    repo = source.get("repo", "") or source.get("url", "")
    # Reject full URLs -- the url fallback accepts owner/repo strings only
    if "://" in repo:
        raise ValueError(
            f"Invalid git-subdir source: expected 'owner/repo' but got a URL '{repo}'. "
            f"Use source type 'url' for full URL references."
        )
    ref = source.get("ref", "")
    subdir = (source.get("subdir", "") or source.get("path", "")).strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid git-subdir source: 'repo' (or 'url') must be 'owner/repo', got '{repo}'"
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

    source_type = _coerce_dict_plugin_type(source)
    if not source_type:
        raise ValueError(
            f"Plugin '{plugin.name}' has dict source with no 'type' and no inferrable 'repo' field"
        )

    if source_type == "github":
        return _resolve_github_source(source)
    elif source_type == "url":
        return _resolve_url_source(source)
    elif source_type == "git-subdir":
        return _resolve_git_subdir_source(source)
    elif source_type == "gitlab":
        # GitLab-native marketplace entries mirror git-subdir (repo + path/subdir).
        return _resolve_git_subdir_source(source)
    elif source_type == "npm":
        raise ValueError(
            f"Plugin '{plugin.name}' uses npm source type which is not supported by APM. "
            f"APM requires Git-based sources. "
            f"Consider asking the marketplace maintainer to add a 'github' source."
        )
    else:
        raise ValueError(f"Plugin '{plugin.name}' has unsupported source type: '{source_type}'")


def resolve_marketplace_plugin(
    plugin_name: str,
    marketplace_name: str,
    *,
    version_spec: str | None = None,
    auth_resolver: object | None = None,
    warning_handler: Callable[[str], None] | None = None,
) -> MarketplacePluginResolution:
    """Resolve a marketplace plugin reference to a canonical string and plugin row.

    For non-GitHub, non-ADO marketplace hosts and in-marketplace subdirectory plugins,
    also returns :attr:`MarketplacePluginResolution.dependency_reference` so callers
    clone the marketplace project only and use ``virtual_path`` for the plugin directory.

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
        :class:`MarketplacePluginResolution` (iterates as ``(canonical, plugin)``).

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

    dep_ref: DependencyReference | None = None
    if _marketplace_host_needs_explicit_git_path(source.host) and _is_in_marketplace_source(
        plugin, source
    ):
        in_repo_path, path_ref = _extract_in_repo_path_and_ref(
            plugin, plugin_root=manifest.plugin_root
        )
        if in_repo_path:
            dep_ref = _gitlab_in_marketplace_dependency_reference(
                source, in_repo_path, version_spec or path_ref
            )
            canonical = dep_ref.to_canonical()

    # ---- Raw ref override ----
    # When version_spec is provided it is treated as a raw git ref that
    # overrides whatever ref came from the marketplace source field.
    if version_spec and dep_ref is None:
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
            marketplace_name,
            plugin_name,
            current_ref,
            version=plugin_version,
        )
        if previous_ref is not None:
            _emit_warning(
                f"Plugin {plugin_name}@{marketplace_name} ref changed: was '{previous_ref}', now '{current_ref}'. "
                "This may indicate a ref swap attack."
            )
        record_ref_pin(
            marketplace_name,
            plugin_name,
            current_ref,
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

        shadows = detect_shadows(plugin_name, marketplace_name, auth_resolver=auth_resolver)
        for shadow in shadows:
            _emit_warning(
                f"Plugin '{plugin_name}' also found in marketplace '{shadow.marketplace_name}'. "
                "Verify you are installing from the intended source."
            )
    except Exception:
        # Shadow detection must never break installation
        logger.debug("Shadow detection failed", exc_info=True)

    return MarketplacePluginResolution(
        canonical=canonical, plugin=plugin, dependency_reference=dep_ref
    )
