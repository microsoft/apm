"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse

from ....utils.github_host import (
    is_artifactory_path,
    is_azure_devops_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    parse_artifactory_path,
    unsupported_host_error,
)
from ....utils.path_security import (
    validate_path_segments,
)
from ...validation import InvalidVirtualPackageExtensionError

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


@classmethod
def _validate_ado_path(
    cls, path_parts: list[str], hostname: str, path: str
) -> tuple[list[str], str | None]:
    """Validate and extract ADO path components and virtual path.

    Returns:
        (base_path_parts, virtual_path)
    """
    is_vs_legacy = is_visualstudio_legacy_hostname(hostname)
    min_ado_parts = 2 if is_vs_legacy else 3
    if len(path_parts) < min_ado_parts:
        raise ValueError(
            f"Invalid Azure DevOps repository path: expected 'org/project/repo', got '{path}'"
        )

    url_virtual_path: str | None = None
    if len(path_parts) > min_ado_parts:
        ado_virtual = "/".join(path_parts[min_ado_parts:])
        validate_path_segments(ado_virtual, context="virtual path")

        if any(ado_virtual.endswith(ext) for ext in cls.REMOVED_COLLECTION_EXTENSIONS):
            raise ValueError(
                f".collection.yml is no longer supported. "
                f"Convert '{ado_virtual}' to an apm.yml with a "
                f"'dependencies' section. "
                f"See: https://microsoft.github.io/apm/guides/dependencies/"
            )

        if any(ado_virtual.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            pass
        else:
            last_segment = ado_virtual.split("/")[-1]
            if "." in last_segment:
                raise InvalidVirtualPackageExtensionError(
                    f"Invalid virtual package path '{ado_virtual}'. "
                    f"Individual files must end with one of: "
                    f"{', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
                    f"For subdirectory packages, the path should not have a file extension."
                )

        url_virtual_path = ado_virtual
        path_parts = path_parts[:min_ado_parts]

    if is_vs_legacy:
        vs_org = hostname.split(".")[0]
        path_parts = [vs_org, *path_parts]

    return path_parts, url_virtual_path


@classmethod
def _validate_non_ado_path(cls, path_parts: list[str], path: str) -> None:
    """Validate non-ADO path components."""
    if len(path_parts) < 2:
        raise ValueError(f"Invalid repository path: expected at least 'user/repo', got '{path}'")
    for pp in path_parts:
        if any(pp.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            raise ValueError(
                f"Invalid repository path: '{path}' contains a virtual file extension. "
                f"Use the dict format with 'path:' for virtual packages in HTTPS URLs"
            )


@classmethod
def _validate_path_components(cls, path_parts: list[str], is_ado_host: bool) -> None:
    """Validate individual path components."""
    allowed_pattern = r"^[a-zA-Z0-9._\- ]+$" if is_ado_host else r"^[a-zA-Z0-9._~-]+$"
    validate_path_segments(
        "/".join(path_parts),
        context="repository URL path",
        reject_empty=True,
    )
    for part in path_parts:
        if not re.match(allowed_pattern, part):
            raise ValueError(f"Invalid repository path component: {part}")


@classmethod
def _validate_url_repo_path(cls, parsed_url) -> tuple[str, str | None]:
    """Validate and normalise the repository path from a parsed URL.

    Checks host support, strips ``.git`` suffixes, removes ``_git``
    segments, and validates each path component against the allowed
    character set for the detected host type.

    For Azure DevOps URLs with extra path segments beyond
    ``org/project/repo`` (e.g.
    ``https://dev.azure.com/org/proj/_git/repo/sub/path``), the extra
    segments are extracted as a virtual package path and validated with
    the same rules as the shorthand virtual-path detector.

    Returns:
        ``(repo_url, virtual_path)`` where *repo_url* is the normalised
        base repository path (e.g. ``owner/repo`` or
        ``org/project/repo``) and *virtual_path* is ``None`` unless
        extra ADO sub-path segments were detected.
    """
    hostname = parsed_url.hostname or ""
    if not is_supported_git_host(hostname):
        raise ValueError(unsupported_host_error(hostname or parsed_url.netloc))

    path = parsed_url.path.strip("/")
    if not path:
        raise ValueError("Repository path cannot be empty")

    if path.endswith(".git"):
        path = path[:-4]

    path_parts = [urllib.parse.unquote(p) for p in path.split("/")]
    if "_git" in path_parts:
        git_idx = path_parts.index("_git")
        path_parts = path_parts[:git_idx] + path_parts[git_idx + 1 :]

    is_ado_host = is_azure_devops_hostname(hostname)
    url_virtual_path: str | None = None

    if is_ado_host:
        path_parts, url_virtual_path = cls._validate_ado_path(path_parts, hostname, path)
    else:
        cls._validate_non_ado_path(path_parts, path)

    cls._validate_path_components(path_parts, is_ado_host)

    return "/".join(path_parts), url_virtual_path


@classmethod
def _validate_final_repo_fields(cls, host, repo_url):
    """Validate the final repo_url and extract ADO organisation fields.

    Performs character-set and segment-count validation appropriate for
    the detected host type (Azure DevOps vs generic git host).

    Returns:
        ``(ado_organization, ado_project, ado_repo)`` -- all ``None``
        for non-ADO hosts.
    """
    is_ado_final = host and is_azure_devops_hostname(host)
    if is_ado_final:
        if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._\- ]+/[a-zA-Z0-9._\- ]+$", repo_url):
            raise ValueError(
                f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
            )
        ado_parts = repo_url.split("/")
        validate_path_segments(repo_url, context="Azure DevOps repository path")
        return ado_parts[0], ado_parts[1], ado_parts[2]

    segments = repo_url.split("/")
    if len(segments) < 2:
        raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")
    # Allow tilde for Bitbucket DC / Sourcehut personal repos (~username)
    if not all(re.match(r"^[a-zA-Z0-9._~-]+$", s) for s in segments):
        raise ValueError(f"Invalid repository format: {repo_url}. Contains invalid characters")
    validate_path_segments(repo_url, context="repository path")
    for seg in segments:
        if any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            raise ValueError(
                f"Invalid repository format: '{repo_url}' contains a virtual file extension. "
                f"Use the dict format with 'path:' for virtual packages in SSH/HTTPS URLs"
            )
    return None, None, None


@staticmethod
def _extract_artifactory_prefix(dependency_str, host):
    """Extract the Artifactory VCS prefix from the original dependency string.

    Returns:
        The prefix string (e.g. ``"artifactory/github"``) or ``None``.
    """
    _art_str = dependency_str.split("#")[0].split("@")[0]
    # Strip scheme if present (e.g., https://host/artifactory/...)
    if "://" in _art_str:
        _art_str = _art_str.split("://", 1)[1]
    _art_segs = _art_str.replace(f"{host}/", "", 1).split("/")
    if is_artifactory_path(_art_segs):
        art_result = parse_artifactory_path(_art_segs)
        if art_result:
            return art_result[0]
    return None
