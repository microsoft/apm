"""Marketplace doctor: network-heavy health checks for a registered marketplace.

Complements :mod:`validator` (local schema checks) with checks that require
resolving each plugin's own ``apm.yml`` at its pinned ref.

The main check (issue #847) looks for transitive APM dependencies that use
direct repo paths instead of marketplace refs.  Such dependencies resolve
via git clone and track HEAD -- **bypassing the marketplace catalogue's
version pinning** -- which defeats the supply-chain guarantee the
marketplace is supposed to provide for consumers.

This module contains only pure logic and an opt-in network fetcher; the CLI
wiring lives in :mod:`apm_cli.commands.marketplace`.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import yaml

from ..utils.path_security import PathTraversalError, validate_path_segments
from .client import fetch_raw
from .errors import MarketplaceError
from .models import MarketplaceManifest, MarketplacePlugin, MarketplaceSource
from .resolver import parse_marketplace_ref

logger = logging.getLogger(__name__)


class DepClassification(enum.Enum):
    """How a dependency string resolves from the consumer's perspective."""

    MARKETPLACE = "marketplace"
    LOCAL = "local"
    BYPASSES_MARKETPLACE = "bypasses_marketplace"
    EMPTY = "empty"


class FetchStatus(enum.Enum):
    """Outcome of fetching a single plugin's apm.yml."""

    OK = "ok"
    NO_MANIFEST = "no_manifest"
    UNSUPPORTED_SOURCE = "unsupported_source"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class DepIssue:
    dep: str
    classification: DepClassification
    suggestion: str


@dataclass(frozen=True)
class PluginDepReport:
    plugin_name: str
    fetch_status: FetchStatus
    issues: Tuple[DepIssue, ...] = ()
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def classify_dependency(dep: str) -> DepClassification:
    """Classify an entry from ``dependencies.apm`` of a plugin's ``apm.yml``.

    Uses :func:`parse_marketplace_ref` as the authority for marketplace-ref
    grammar (so behavior stays in sync if that grammar evolves), and
    ``DependencyReference.is_local_path`` for local paths.  Anything else
    hits a git remote directly, which is what issue #847 warns against.
    """
    stripped = (dep or "").strip()
    if not stripped:
        return DepClassification.EMPTY

    try:
        if parse_marketplace_ref(stripped) is not None:
            return DepClassification.MARKETPLACE
    except ValueError:
        # Semver range in marketplace ref -- still marketplace-shaped intent.
        # Let the normal install path surface that error; doctor does not
        # relitigate grammar.
        return DepClassification.MARKETPLACE

    from ..models.dependency.reference import DependencyReference

    if DependencyReference.is_local_path(stripped):
        return DepClassification.LOCAL

    return DepClassification.BYPASSES_MARKETPLACE


def _suggest_replacement(dep: str) -> str:
    """Best-effort suggestion text for a bypassing dep."""
    base = dep.split("#", 1)[0]
    pkg_hint = base.rsplit("/", 1)[-1] if "/" in base else base
    if pkg_hint.endswith(".git"):
        pkg_hint = pkg_hint[: -len(".git")]
    pkg_hint = pkg_hint.strip() or "package"
    return f"publish via marketplace and depend on it as '{pkg_hint}@<marketplace>'"


def _normalize_dep_entry(entry) -> Optional[str]:
    """Flatten a dep entry to the string shape expected by the classifier.

    Matches :meth:`DependencyReference.parse_from_dict`: a dict entry is
    either ``{git: URL, path?: SUB, ref?: REF}`` (direct git URL -- bypasses
    the marketplace) or ``{path: ./local}`` (local filesystem path).

    Returns the canonical string form, or ``None`` if the entry cannot be
    flattened (e.g. bare primitives we do not recognise).
    """
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None

    if "git" in entry:
        git_url = entry.get("git")
        if not isinstance(git_url, str) or not git_url.strip():
            return None
        # For classification we only care about the scheme/host shape, not
        # the sub-path or ref.  classify_dependency will inspect this and
        # see that it is not a ``name@marketplace`` ref, flagging it.
        return git_url.strip()

    if "path" in entry:
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()

    return None


def _collect_apm_dep_strings(apm_yml_data: dict) -> List[str]:
    """Collect dep entries from both dependencies and devDependencies.

    Handles both string and dict entry forms (the two apm.yml supports) so
    the classifier does not miss ``{git: ...}`` object-style deps, which
    also bypass the marketplace.
    """
    results: List[str] = []
    for section_name in ("dependencies", "devDependencies"):
        section = apm_yml_data.get(section_name)
        if not isinstance(section, dict):
            continue
        apm_list = section.get("apm")
        if not isinstance(apm_list, list):
            continue
        for entry in apm_list:
            flat = _normalize_dep_entry(entry)
            if flat is not None:
                results.append(flat)
    return results


# ---------------------------------------------------------------------------
# Plugin source resolution (github-dict sources only, matches resolver.py)
# ---------------------------------------------------------------------------


def _resolve_plugin_github_coords(
    plugin: MarketplacePlugin,
    fallback_host: str,
) -> Optional[Tuple[str, str, str, str, str]]:
    """Extract ``(host, owner, repo, ref, apm_yml_path)`` for a plugin.

    Returns ``None`` for plugin sources the doctor cannot address (string
    sources, non-github dict types, malformed entries).  A returned value
    is safe to hand to :func:`fetch_raw`; path components have been
    traversal-checked.
    """
    source = plugin.source
    if not isinstance(source, dict):
        return None
    if source.get("type") != "github":
        return None
    repo = source.get("repo", "")
    if not isinstance(repo, str) or "/" not in repo:
        return None
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    ref = source.get("ref") or "HEAD"
    host = source.get("host") or fallback_host or "github.com"
    path = (source.get("path") or "").strip("/")
    if path:
        try:
            validate_path_segments(path, context="plugin source path")
        except PathTraversalError:
            return None
        apm_yml_path = f"{path}/apm.yml"
    else:
        apm_yml_path = "apm.yml"
    return host, owner, name, ref, apm_yml_path


# ---------------------------------------------------------------------------
# Fetchers and orchestration
# ---------------------------------------------------------------------------


def fetch_plugin_apm_yml(
    plugin: MarketplacePlugin,
    marketplace_source: MarketplaceSource,
    auth_resolver: Optional[object] = None,
) -> Tuple[FetchStatus, Optional[dict], str]:
    """Fetch and parse a plugin's ``apm.yml`` at its pinned ref.

    Returns a tuple ``(status, data_or_None, detail_message)``.  Never
    raises -- every failure is reported through the status enum so that a
    single bad plugin cannot abort a whole-marketplace doctor run.
    """
    coords = _resolve_plugin_github_coords(plugin, marketplace_source.host)
    if coords is None:
        return (
            FetchStatus.UNSUPPORTED_SOURCE,
            None,
            "plugin source is not an addressable github manifest",
        )

    host, owner, name, ref, apm_yml_path = coords
    try:
        raw = fetch_raw(
            host,
            owner,
            name,
            apm_yml_path,
            ref,
            auth_resolver=auth_resolver,
        )
    except MarketplaceError as exc:
        return FetchStatus.NETWORK_ERROR, None, str(exc)

    if raw is None:
        return (
            FetchStatus.NO_MANIFEST,
            None,
            f"no apm.yml at '{apm_yml_path}' @ {ref}",
        )

    try:
        data = yaml.safe_load(raw.decode("utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        return FetchStatus.PARSE_ERROR, None, f"malformed YAML: {exc}"

    if not isinstance(data, dict):
        return FetchStatus.PARSE_ERROR, None, "apm.yml root is not a mapping"

    return FetchStatus.OK, data, ""


def check_plugin(
    plugin: MarketplacePlugin,
    marketplace_source: MarketplaceSource,
    auth_resolver: Optional[object] = None,
    *,
    _fetcher: Optional[Callable] = None,
) -> PluginDepReport:
    """Run doctor checks against a single plugin.

    ``_fetcher`` is a test seam with the same signature as
    :func:`fetch_plugin_apm_yml`.
    """
    fetcher = _fetcher or fetch_plugin_apm_yml
    status, data, detail = fetcher(plugin, marketplace_source, auth_resolver)
    if status != FetchStatus.OK or data is None:
        return PluginDepReport(
            plugin_name=plugin.name,
            fetch_status=status,
            detail=detail,
        )

    issues: List[DepIssue] = []
    for dep in _collect_apm_dep_strings(data):
        cls = classify_dependency(dep)
        if cls == DepClassification.BYPASSES_MARKETPLACE:
            issues.append(
                DepIssue(
                    dep=dep,
                    classification=cls,
                    suggestion=_suggest_replacement(dep),
                )
            )
    return PluginDepReport(
        plugin_name=plugin.name,
        fetch_status=FetchStatus.OK,
        issues=tuple(issues),
    )


def run_doctor(
    manifest: MarketplaceManifest,
    marketplace_source: MarketplaceSource,
    auth_resolver: Optional[object] = None,
    *,
    _fetcher: Optional[Callable] = None,
) -> List[PluginDepReport]:
    """Run all doctor checks across every plugin of a marketplace manifest.

    Each plugin is isolated: a failure inside one check produces a
    :class:`PluginDepReport` with a non-``OK`` status rather than
    propagating.
    """
    reports: List[PluginDepReport] = []
    for plugin in manifest.plugins:
        try:
            reports.append(
                check_plugin(
                    plugin,
                    marketplace_source,
                    auth_resolver,
                    _fetcher=_fetcher,
                )
            )
        except Exception as exc:  # pragma: no cover -- defensive
            logger.warning(
                "Doctor check failed for plugin '%s': %s", plugin.name, exc
            )
            reports.append(
                PluginDepReport(
                    plugin_name=plugin.name,
                    fetch_status=FetchStatus.NETWORK_ERROR,
                    detail=f"unexpected error: {exc}",
                )
            )
    return reports
