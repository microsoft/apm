"""JFrog Artifactory VCS-proxy path detection and archive URL building.

Private module — import via ``apm_cli.utils.github_host`` for the public API.
All symbols here are re-exported from that module so callers do not need to
reference this path directly.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _ArtifactoryArchiveOpts:
    """Options for Artifactory archive URL building."""

    ref: str = "main"
    scheme: str = "https"


def is_artifactory_path(path_segments: list) -> bool:
    """Return True if path segments indicate a JFrog Artifactory VCS repository.

    Artifactory VCS paths follow the pattern: artifactory/{repo-key}/{owner}/{repo}
    Detection: first segment is 'artifactory' and there are at least 4 segments.
    """
    return len(path_segments) >= 4 and path_segments[0].lower() == "artifactory"


def parse_artifactory_path(path_segments: list) -> tuple:
    """Parse Artifactory path into (prefix, owner, repo, virtual_path).

    Input:  ['artifactory', 'github', 'microsoft', 'apm-sample-package']
    Output: ('artifactory/github', 'microsoft', 'apm-sample-package', None)

    Input:  ['artifactory', 'github', 'owner', 'repo', 'skills', 'review']
    Output: ('artifactory/github', 'owner', 'repo', 'skills/review')

    Returns None if not a valid Artifactory path.
    """
    if not is_artifactory_path(path_segments):
        return None
    repo_key = path_segments[1]
    remaining = path_segments[2:]
    prefix = f"artifactory/{repo_key}"
    owner = remaining[0]
    repo = remaining[1]
    virtual_path = "/".join(remaining[2:]) if len(remaining) > 2 else None
    return (prefix, owner, repo, virtual_path)


def build_artifactory_archive_url(
    host: str,
    prefix: str,
    owner: str,
    repo: str,
    *,
    ref: str = "main",
    scheme: str = "https",
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
    return (
        # GitHub-style: /archive/refs/heads/{ref}.zip
        f"{base}/archive/refs/heads/{ref}.zip",
        # GitLab-style: /-/archive/{ref}/{repo}-{ref}.zip
        f"{base}/-/archive/{ref}/{repo}-{ref}.zip",
        # GitHub-style tags fallback
        f"{base}/archive/refs/tags/{ref}.zip",
        # codeload.github.com-style: /zip/refs/heads/{ref}
        # Required when Artifactory upstream is configured as codeload.github.com
        # (workaround for private repos where github.com redirects to codeload with tokens
        # that Artifactory cannot follow across hosts)
        f"{base}/zip/refs/heads/{ref}",
        f"{base}/zip/refs/tags/{ref}",
    )
