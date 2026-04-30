"""Shared utility functions for integration modules."""


def normalize_repo_url(package_repo_url: str) -> str:
    """Normalize a repo URL to owner/repo format.

    Handles various URL formats:
    - Full URLs: https://github.com/owner/repo -> owner/repo
    - With .git suffix: owner/repo.git -> owner/repo
    - Short form: owner/repo -> owner/repo (unchanged)

    Args:
        package_repo_url: Repository URL in any format

    Returns:
        str: Normalized owner/repo format

    Examples:
        >>> normalize_repo_url("https://github.com/owner/repo")
        'owner/repo'
        >>> normalize_repo_url("https://github.com/owner/repo.git")
        'owner/repo'
        >>> normalize_repo_url("owner/repo")
        'owner/repo'
    """
    if "://" not in package_repo_url:
        # Already in short form, just remove .git suffix and trailing slashes
        normalized = package_repo_url
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        return normalized.rstrip("/")

    # Extract owner/repo from full URL: https://github.com/owner/repo -> owner/repo
    parts = package_repo_url.split("://", 1)[1]  # Remove protocol
    if "/" in parts:
        path_parts = parts.split("/", 1)  # Split host from path
        if len(path_parts) > 1:
            normalized = path_parts[1]
            # Remove trailing slashes first (e.g., "owner/repo.git/" -> "owner/repo.git")
            normalized = normalized.rstrip("/")
            # Then remove .git suffix if present
            if normalized.endswith(".git"):
                normalized = normalized[:-4]
            return normalized

    return package_repo_url
