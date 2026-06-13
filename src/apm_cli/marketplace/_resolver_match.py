"""Cross-repo misconfig detection and in-marketplace source-matching helpers.

Extracted from resolver.py to keep module complexity bounded.
All symbols are re-exported from resolver.py so existing import paths
(tests, patches) keep working unchanged.

No module-level import of resolver.py (cycle-safe).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..utils.github_host import (
    is_azure_devops_hostname,
    is_github_hostname,
    is_supported_git_host,
)

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference
    from .models import MarketplacePlugin, MarketplaceSource


# ---------------------------------------------------------------------------
# CrossRepoMisconfigRisk sentinel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossRepoMisconfigRisk:
    """Signal that a cross-repo dict ``type: github`` source on an enterprise
    GitHub-family marketplace declares a bare ``owner/repo`` whose canonical
    falls back to ``github.com`` -- the same syntactic ambiguity that powers
    a dependency-confusion attack (#1326, formerly diagnosed only as #1305).

    Attached to :class:`MarketplacePluginResolution` when the marketplace is on
    ``*.ghe.com`` and the plugin's dict source declares a bare ``owner/repo``
    that does not match the marketplace project. The resolver deliberately
    leaves these canonicals bare (PR #1292 scoped its host backfill to
    in-marketplace sources), so ``DependencyReference.parse`` defaults the host
    to ``github.com``. Two intents share this syntax -- a legitimate cross-host
    ``github.com`` open-source dep, or a misconfigured same-host entry that
    should have been ``corp.ghe.com/owner/repo`` -- and the resolver cannot
    distinguish them.

    Consumer contract (#1326): the install command consults this sentinel
    BEFORE any outbound validation HTTP call and refuses the package
    fail-closed when it is non-``None``. The earlier #1305 design surfaced
    only an advisory hint on validation failure, which left the success
    path (attacker pre-stages the bare namespace on public github.com)
    silently exploitable. Cross-host explicit qualification by the
    marketplace author -- ``repo: github.com/owner/repo`` -- prevents
    the sentinel from attaching at the resolver layer (see
    :func:`_compute_cross_repo_misconfig_risk`), which is the supported
    escape hatch for declared cross-host intent.
    """

    marketplace_host: str
    bare_repo_field: str
    suggested_qualified_repo: str


# ---------------------------------------------------------------------------
# Owner/repo slug normalisation
# ---------------------------------------------------------------------------


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
    """Per spec §Interface Contract -- in-marketplace detection."""
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


# ---------------------------------------------------------------------------
# Host-routing helpers
# ---------------------------------------------------------------------------


def _marketplace_host_needs_explicit_git_path(host: str) -> bool:
    """True when in-repo marketplace plugins must use ``git`` + ``path`` (clone root + subdir).

    ``github.com`` and ``*.ghe.com`` virtual shorthand is reliable. Azure DevOps uses
    a different URL shape and is excluded. Self-managed GitLab FQDNs are often
    classified as ``generic`` by :meth:`AuthResolver.classify_host` when not listed in
    ``GITLAB_HOST`` / ``APM_GITLAB_HOSTS`` -- they still need explicit clone URLs so
    paths like ``registry/pkg`` are not treated as extra project namespace segments.
    """
    if not host or not str(host).strip():
        return False
    h = str(host).strip().split("/", 1)[0]
    if is_azure_devops_hostname(h):
        return False
    return not is_github_hostname(h)


def _source_needs_explicit_git_path(source: MarketplaceSource) -> bool:
    """Kind-aware variant of :func:`_marketplace_host_needs_explicit_git_path`.

    For URL-first sources, the ``kind`` derivation already encodes the routing
    decision: any host APM doesn't classify as github-family needs the explicit
    git+path canonical (mirrors the existing GitLab self-managed pattern), and
    that now includes Azure DevOps and generic git hosts since their
    ``marketplace.json`` is fetched via subprocess git instead of an API.

    Local marketplaces handle relative sources via :func:`_resolve_local_relative_source`
    on the fast path and never reach this helper.
    """
    kind = source.kind
    if kind == "github":
        return False
    if kind in ("gitlab", "git"):
        return True
    # Fall back to legacy host-based behaviour for any kind we don't recognise
    return _marketplace_host_needs_explicit_git_path(source.host)


def _needs_canonical_host_prefix(canonical: str, host: str) -> bool:
    """True when a GitHub-family enterprise host must be prefixed to ``canonical``.

    GitHub-family hosts (``github.com`` + ``*.ghe.com``) keep virtual shorthand --
    ``resolve_plugin_source`` emits a bare ``owner/repo[/path]`` canonical because
    there is no nested-group ambiguity to disambiguate. ``DependencyReference.parse``
    defaults missing hosts to ``github.com``, which is correct for ``github.com`` but
    silently mis-routes auth for every ``*.ghe.com`` marketplace.

    Returns True only for enterprise GitHub hosts (``*.ghe.com``) so the caller can
    backfill the host while preserving shorthand semantics. Idempotent: when the
    canonical already starts with ``host`` (case-insensitive) -- as happens when the
    manifest's dict source carries a host-qualified ``repo`` -- this returns False
    so the prefix is not duplicated.

    GHES (GitHub Enterprise Server, configured via ``GITHUB_HOST``) is not handled
    here. Those hosts return True from ``_marketplace_host_needs_explicit_git_path``
    (neither GitHub-family nor ADO) so ``resolve_marketplace_plugin`` builds a
    structured ``dep_ref`` upstream and this helper is never reached. The
    ``is_github_hostname`` check below is defense-in-depth that would also reject
    them if a future change ever bypassed the upstream guard.

    Also returns False when ``canonical`` is in URL form (``https://...``) or SSH
    SCP shorthand (``git@host:owner/repo``). Manifests that put a full URL in the
    ``repo`` field reach this point via ``_resolve_github_source`` (which only
    requires a ``/``); detecting those by ``":"`` in the first slash-split segment
    avoids producing malformed ``host/https://...`` canonicals. Those forms already
    carry a host and ``DependencyReference.parse`` resolves them natively.
    """
    h = (host or "").strip()
    if not h or not is_github_hostname(h) or h.lower() == "github.com":
        return False
    first_segment = canonical.split("/", 1)[0]
    if ":" in first_segment:
        return False
    return first_segment.lower() != h.lower()


# ---------------------------------------------------------------------------
# Cross-repo misconfig risk computation
# ---------------------------------------------------------------------------


def _cross_repo_early_exit(
    plugin: MarketplacePlugin,
    source: MarketplaceSource,
    canonical: str,
    dep_ref: DependencyReference | None,
) -> bool:
    """Return True when ``_compute_cross_repo_misconfig_risk`` should short-circuit.

    Consolidates the five guard conditions that all lead to ``return None`` in
    the parent so it stays within the PLR0911 return-statement budget.
    """
    if dep_ref is not None:
        return True
    if not isinstance(plugin.source, dict):
        return True
    if _coerce_dict_plugin_type(plugin.source) != "github":
        return True
    if _is_in_marketplace_source(plugin, source):
        return True
    return not _needs_canonical_host_prefix(canonical, source.host)


def _compute_cross_repo_misconfig_risk(
    plugin: MarketplacePlugin,
    source: MarketplaceSource,
    canonical: str,
    dep_ref: DependencyReference | None,
) -> CrossRepoMisconfigRisk | None:
    """Identify the #1305 misconfiguration: cross-repo dict ``type: github``
    source with bare ``repo`` on an enterprise GitHub-family marketplace.

    Returns a :class:`CrossRepoMisconfigRisk` when **all** of:

    - ``dep_ref`` is ``None`` (GitHub-family virtual-shorthand path; GitLab and
      self-managed FQDNs build a structured ref upstream and sidestep the bug)
    - ``plugin.source`` is a dict whose normalized type is ``github`` (other
      dict types -- ``gitlab``, ``git-subdir`` -- hit the same auth-routing
      bug but the "host-qualify with marketplace host" remediation only
      matches operator intent for the GitHub family)
    - the source is **not** an in-marketplace reference (PR #1292 already
      backfills the host for those)
    - ``_needs_canonical_host_prefix`` agrees the canonical is bare and the
      host is GitHub-family enterprise (``*.ghe.com``; idempotent against
      already host-qualified, URL, and SSH forms)
    - the ``repo`` field is a non-empty ``owner/repo`` shorthand

    Otherwise returns ``None``. Pure -- no logging, no side effects.
    """
    if _cross_repo_early_exit(plugin, source, canonical, dep_ref):
        return None
    repo_field = plugin.source.get("repo", "")  # type: ignore[union-attr]
    if not isinstance(repo_field, str):
        return None
    bare = repo_field.strip().lstrip("/")
    if "/" not in bare:
        return None
    # #1326: an already-host-qualified `repo:` field declares explicit intent
    # (e.g. ``repo: github.com/owner/repo`` on a ``*.ghe.com`` marketplace is
    # an unambiguous declared cross-host dependency). Only the truly-bare
    # ``owner/repo`` form is the dependency-confusion vector this sentinel
    # flags. ``_needs_canonical_host_prefix`` above already returns False
    # for SAME-host qualification (its idempotency clause) and for URL /
    # SSH SCP shorthand canonicals; this is the symmetric guard for the
    # remaining case -- CROSS-host shorthand qualification (``github.com/...``
    # on a ``*.ghe.com`` marketplace), which the idempotency check cannot
    # detect because the canonical starts with a different host than
    # ``source.host``.
    #
    # Defense in depth: extract the host from URL and SCP shorthand forms
    # too, so the guard is robust even if a future upstream refactor lets
    # those forms reach this point.
    explicit_host = ""
    bare_lower = bare.lower()
    if bare_lower.startswith(("https://", "http://", "ssh://")):
        explicit_host = (urlparse(bare).hostname or "").strip()
    elif bare.startswith("git@") and ":" in bare:
        # SCP shorthand: ``git@host:owner/repo``
        explicit_host = bare[4:].split(":", 1)[0].strip()
    else:
        explicit_host = bare.split("/", 1)[0]
    # ``is_supported_git_host`` accepts any valid FQDN, not an allowlist.
    if is_supported_git_host(explicit_host):
        return None
    return CrossRepoMisconfigRisk(
        marketplace_host=source.host,
        bare_repo_field=bare,
        suggested_qualified_repo=f"{source.host}/{bare}",
    )
