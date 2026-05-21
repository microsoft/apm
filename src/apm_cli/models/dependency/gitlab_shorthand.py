"""GitLab shorthand helpers for DependencyReference."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...utils.github_host import (
    is_gitlab_hostname,
    is_supported_git_host,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
)

if TYPE_CHECKING:
    from .reference import DependencyReference


def _split_gitlab_direct_shorthand_parts(
    cls, package: str
) -> tuple[str, list[str], str | None] | None:
    """If *package* is bare host/path shorthand, return (host, path_segments, ref_str).

    Returns ``None`` for ``https://``, ``git@``, or nonâ€“GitLab-class hosts.
    """
    s = package.strip()
    ref_out: str | None = None
    if "#" in s:
        s, r = s.rsplit("#", 1)
        s = s.strip()
        r = r.strip()
        ref_out = r if r else None
    maybe_raise_bare_fqdn_github_gitlab_conflict(package)
    if s.startswith(("git@", "https://", "http://", "ssh://", "//")):
        return None
    if "/" not in s:
        return None
    parts = s.split("/")
    host_cand = parts[0]
    if "." not in host_cand:
        return None
    segs = [p for p in parts[1:] if p]
    if len(segs) < 1:
        return None
    if not is_supported_git_host(host_cand) or not is_gitlab_hostname(host_cand):
        return None
    return (host_cand, segs, ref_out)


def _needs_gitlab_direct_shorthand_probing(cls, package: str, dep_ref: DependencyReference) -> bool:
    """True when install should probe left-to-right repo boundaries (GitLab only)."""
    if dep_ref.is_local:
        return False
    if dep_ref.is_virtual:
        return False
    sp = cls.split_gitlab_direct_shorthand_parts(package)
    if not sp:
        return False
    _host, segs, _ref = sp
    return len(segs) >= 3


def _iter_gitlab_direct_shorthand_boundary_candidates(cls, path_segments: list[str]):
    """Yield (repo_url, virtual_suffix) for k=2..n-1 (earliest k first)."""
    n = len(path_segments)
    if n < 3:
        return
    for k in range(2, n):
        repo = "/".join(path_segments[:k])
        suffix = "/".join(path_segments[k:])
        if cls.virtual_suffix_is_installable_shape(suffix):
            yield repo, suffix


def _from_gitlab_shorthand_probe(
    cls,
    host: str,
    repo_url: str,
    virtual_path: str,
    reference: str | None,
) -> DependencyReference:
    """Build a virtual dependency ref for a resolved GitLab shorthand probe."""
    return cls(
        repo_url=repo_url,
        host=host,
        reference=reference,
        virtual_path=virtual_path,
        is_virtual=True,
    )


def _gitlab_shorthand_repo_segment_count(
    cls,
    path_segments: list[str],
    has_virtual_ext: bool,
    has_collection: bool,
) -> int:
    """Return how many segments after the host belong to the GitLab project path.

    GitLab allows nested groups; unlike GitHub's fixed ``owner/repo``, the
    project slug may span 3+ segments. Virtual package shorthand must not
    chop a nested group path after two segments.

    Shorthand cannot disambiguate every deep namespace; ambiguous cases use
    object form with ``git:`` + ``path:`` in ``apm.yml``.

    This does **not** split extension-less paths (e.g. ``.../registry/pkg``)
    into repo + virtual: that would mis-parse valid 5+ segment project
    paths; use ``parse_from_dict`` with an explicit ``path`` for those.
    """
    n = len(path_segments)
    if n < 2:
        return n

    if has_collection and "collections" in path_segments:
        coll_idx = path_segments.index("collections")
        if coll_idx >= 2:
            return coll_idx
        return n

    if has_virtual_ext:
        for idx, seg in enumerate(path_segments):
            if idx >= 2 and seg in cls._GITLAB_VIRTUAL_ROOT_SEGMENTS:
                return idx
        if n == 3:
            return 2
        if n == 4:
            return 3
        if n >= 5:
            return 3
        return 2

    return n
