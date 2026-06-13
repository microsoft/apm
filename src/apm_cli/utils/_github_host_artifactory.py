"""JFrog Artifactory URL helpers extracted from :mod:`apm_cli.utils.github_host`.

Extracted to keep ``github_host.py`` under the 800-line threshold while
preserving 100% behavioural equivalence.  This module is private
(``_`` prefix).  All public names are re-exported from ``github_host.py``
so ``apm_cli.utils.github_host.NAME`` continues to resolve correctly.

Rule B note: none of the functions here reference the patched module-level
names from ``github_host`` (``is_github_hostname``, ``is_azure_devops_hostname``,
etc.), so no late-import routing is needed.
"""

from __future__ import annotations


def is_artifactory_path(path_segments: list) -> bool:
    """Return True if path segments indicate a JFrog Artifactory VCS repository.

    Artifactory VCS paths follow the pattern: artifactory/{repo-key}/{owner}/{repo}
    Detection: first segment is 'artifactory' and there are at least 4 segments.
    """
    return len(path_segments) >= 4 and path_segments[0].lower() == "artifactory"


def iter_artifactory_boundary_candidates(path_segments: list, shape_filter=None):
    """Yield ``(prefix, owner, repo, virtual_path)`` candidates shallow-first.

    Mirrors :meth:`DependencyReference.iter_gitlab_direct_shorthand_boundary_candidates`:
    enumerate every plausible (owner, repo) split and let the caller probe each
    one against the Artifactory proxy.  The probe (HEAD on the archive URL)
    decides the real boundary; this iterator only proposes candidates.

    If *shape_filter* is provided, candidates whose ``virtual_path`` fails the
    filter are skipped.  The candidate with no virtual path (``k == n``) is
    always yielded as the all-as-repo fallback so callers that need a
    deterministic answer (no probing) can pick it.

    The ``//`` empty-segment notation explicitly marks the repo / virtual
    boundary and short-circuits the iterator to a single candidate.

    Returns nothing for non-Artifactory paths.
    """
    if not is_artifactory_path(path_segments):
        return
    repo_key = path_segments[1]
    prefix = f"artifactory/{repo_key}"
    remaining = path_segments[2:]
    if not remaining:
        return
    owner = remaining[0]
    after_owner = remaining[1:]
    n = len(after_owner)
    if n == 0:
        return

    if "" in after_owner:
        empty_idx = after_owner.index("")
        repo_parts = after_owner[:empty_idx]
        suffix_parts = [s for s in after_owner[empty_idx + 1 :] if s]
        if repo_parts:
            yield (
                prefix,
                owner,
                "/".join(repo_parts),
                "/".join(suffix_parts) if suffix_parts else None,
            )
        return

    for k in range(1, n + 1):
        repo = "/".join(after_owner[:k])
        suffix_parts = after_owner[k:]
        suffix = "/".join(suffix_parts) if suffix_parts else None
        if suffix is not None and shape_filter is not None and not shape_filter(suffix):
            continue
        yield (prefix, owner, repo, suffix)


def parse_artifactory_path(path_segments: list) -> tuple:
    """Parse Artifactory path into ``(prefix, owner, repo, virtual_path)``.

    Parse-time output is intentionally simple and unambiguous: ``owner`` is the
    first segment after ``artifactory/{key}``, ``repo`` is the next segment,
    and any further segments become ``virtual_path``.  The authoritative
    boundary -- needed for nested GitLab subgroup paths behind the Artifactory
    proxy -- is determined by :func:`apm_cli.install.artifactory_resolver.\
_resolve_artifactory_boundary`, which probes archive URLs and rebuilds the
    dependency reference at the verified boundary.

    The ``//`` notation (empty segment) is honored as an explicit, deterministic
    boundary marker so users can opt out of probing.

    Returns None if not a valid Artifactory path.
    """
    if not is_artifactory_path(path_segments):
        return None
    repo_key = path_segments[1]
    prefix = f"artifactory/{repo_key}"
    remaining = path_segments[2:]
    if not remaining:
        return None
    owner = remaining[0]
    after_owner = remaining[1:]
    if not after_owner:
        return None

    if "" in after_owner:
        empty_idx = after_owner.index("")
        repo_parts = after_owner[:empty_idx]
        suffix_parts = [s for s in after_owner[empty_idx + 1 :] if s]
        if not repo_parts:
            # ``owner//virtual`` has no segments before the explicit boundary,
            # so there is no repo to install -- reject as invalid rather than
            # falling through and returning ``repo=''``.
            return None
        return (
            prefix,
            owner,
            "/".join(repo_parts),
            "/".join(suffix_parts) if suffix_parts else None,
        )

    repo = after_owner[0]
    virtual_path = "/".join(after_owner[1:]) if len(after_owner) > 1 else None
    return (prefix, owner, repo, virtual_path)


def build_artifactory_archive_url(
    host: str, prefix: str, owner: str, repo: str, ref: str = "main", scheme: str = "https"
) -> tuple:
    """Build Artifactory VCS archive download URLs.

    Returns a tuple of URLs to try in order.  Because Artifactory proxies
    the upstream server's native URL scheme, we attempt GitHub-style,
    GitLab-style, and codeload.github.com-style archive paths so the caller
    does not need to know what sits behind the Artifactory remote repository.

    Organizations using private GitHub repositories must configure their
    Artifactory upstream as ``codeload.github.com`` (instead of ``github.com``)
    because Artifactory cannot follow GitHub's cross-host redirect (which
    carries short-lived tokens) to codeload.  When the upstream is
    ``codeload.github.com``, the required archive path is
    ``/{owner}/{repo}/zip/refs/heads/{ref}`` (no ``.zip`` extension).

    Args:
        host: Artifactory hostname (e.g., 'artifactory.example.com')
        prefix: Artifactory path prefix (e.g., 'artifactory/github')
        owner: Repository owner
        repo: Repository name
        ref: Git reference (branch or tag name)
        scheme: URL scheme (default 'https'; 'http' for local dev proxies)

    Returns:
        Tuple of URLs to try in order
    """
    base = f"{scheme}://{host}/{prefix}/{owner}/{repo}"
    # GitLab archive filenames use only the project basename, even when the
    # project sits inside a subgroup (e.g. ``group/sub/pkg`` becomes
    # ``pkg-{ref}.zip``).  ``rsplit`` keeps the flat case unchanged.
    repo_basename = repo.rsplit("/", 1)[-1]
    return (
        # GitHub-style: /archive/refs/heads/{ref}.zip
        f"{base}/archive/refs/heads/{ref}.zip",
        # GitLab-style: /-/archive/{ref}/{basename}-{ref}.zip
        f"{base}/-/archive/{ref}/{repo_basename}-{ref}.zip",
        # GitHub-style tags fallback
        f"{base}/archive/refs/tags/{ref}.zip",
        # codeload.github.com-style: /zip/refs/heads/{ref}
        # Required when Artifactory upstream is configured as codeload.github.com
        # (workaround for private repos where github.com redirects to codeload with tokens
        # that Artifactory cannot follow across hosts)
        f"{base}/zip/refs/heads/{ref}",
        f"{base}/zip/refs/tags/{ref}",
    )
