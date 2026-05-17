"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ....cache.url_normalize import SCP_LIKE_RE
from ....utils.github_host import (
    default_host,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
    parse_artifactory_path,
    unsupported_host_error,
)
from ....utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ...validation import InvalidVirtualPackageExtensionError
from ..types import VirtualPackageType

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


from .core import DependencyReference


@classmethod
def virtual_suffix_is_installable_shape(cls, virtual_path: str) -> bool:
    """Return whether *virtual_path* matches APM virtual package shape rules.

    Used for GitLab direct host/path shorthand: a repo boundary is accepted
    only when the remaining suffix would be a valid virtual path (file,
    collection, or extension-less subdirectory), matching the rules applied
    in :meth:`_detect_virtual_package` for the tail segments.
    """
    if not virtual_path or not virtual_path.strip():
        return False
    v = virtual_path.strip().strip("/")
    try:
        validate_path_segments(v, context="virtual path")
    except PathTraversalError:
        return False
    if "/collections/" in v or v.startswith("collections/"):
        return True
    if any(v.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
        return True
    last = v.split("/")[-1]
    return "." not in last


@classmethod
def _detect_virtual_package(cls, dependency_str: str):
    """Detect whether *dependency_str* refers to a virtual package.

    Returns:
        (is_virtual_package, virtual_path, validated_host)
    """
    # Temporarily remove reference for path segment counting
    temp_str = dependency_str
    if "#" in temp_str:
        temp_str = temp_str.rsplit("#", 1)[0]

    is_virtual_package = False
    virtual_path = None
    validated_host = None

    if temp_str.lower().startswith(("git@", "https://", "http://", "ssh://")):
        return is_virtual_package, virtual_path, validated_host

    check_str = temp_str

    if "/" in check_str:
        first_segment = check_str.split("/")[0]

        if "." in first_segment:
            test_url = f"https://{check_str}"
            try:
                parsed = urllib.parse.urlparse(test_url)
                hostname = parsed.hostname

                if hostname and is_supported_git_host(hostname):
                    validated_host = hostname
                    path_parts = parsed.path.lstrip("/").split("/")
                    if len(path_parts) >= 2:
                        check_str = "/".join(check_str.split("/")[1:])
                else:
                    raise ValueError(unsupported_host_error(hostname or first_segment))
            except (ValueError, AttributeError) as e:
                if isinstance(e, ValueError) and "Invalid Git host" in str(e):
                    raise
                raise ValueError(unsupported_host_error(first_segment)) from e
        elif check_str.startswith("gh/"):
            check_str = "/".join(check_str.split("/")[1:])

    path_segments = [seg for seg in check_str.split("/") if seg]

    is_ado = validated_host is not None and is_azure_devops_hostname(validated_host)
    is_generic_host = (
        validated_host is not None
        and not is_github_hostname(validated_host)
        and not is_azure_devops_hostname(validated_host)
    )
    is_gitlab_host = validated_host is not None and is_gitlab_hostname(validated_host)

    if is_ado and "_git" in path_segments:
        git_idx = path_segments.index("_git")
        path_segments = path_segments[:git_idx] + path_segments[git_idx + 1 :]

    # Detect Artifactory VCS paths (artifactory/{repo-key}/{owner}/{repo})
    is_artifactory = is_generic_host and is_artifactory_path(path_segments)

    if is_ado:
        # *.visualstudio.com encodes org in the subdomain; path is proj/repo (2 parts).
        # dev.azure.com encodes org as the first path segment; path is org/proj/repo (3 parts).
        if validated_host and is_visualstudio_legacy_hostname(validated_host):
            min_base_segments = 2
        else:
            min_base_segments = 3
    elif is_artifactory:
        # Artifactory: artifactory/{repo-key}/{owner}/{repo}
        min_base_segments = 4
    elif is_generic_host:
        has_virtual_ext = any(
            any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS) for seg in path_segments
        )
        has_collection = "collections" in path_segments
        if is_gitlab_host:
            min_base_segments = cls._gitlab_shorthand_repo_segment_count(
                path_segments, has_virtual_ext, has_collection
            )
        elif has_virtual_ext or has_collection:
            min_base_segments = 2
        else:
            min_base_segments = len(path_segments)
    else:
        min_base_segments = 2

    min_virtual_segments = min_base_segments + 1

    if len(path_segments) >= min_virtual_segments:
        is_virtual_package = True
        virtual_path = "/".join(path_segments[min_base_segments:])

        # Security: reject path traversal in virtual path
        validate_path_segments(virtual_path, context="virtual path")

        # Reject removed `.collection.yml` extensions with a clear
        # migration message (#1094). Curated dependency aggregators
        # are now expressed as `apm.yml` with a `dependencies` block.
        if any(virtual_path.endswith(ext) for ext in cls.REMOVED_COLLECTION_EXTENSIONS):
            raise ValueError(
                f".collection.yml is no longer supported. "
                f"Convert '{virtual_path}' to an apm.yml with a "
                f"'dependencies' section. "
                f"See: https://microsoft.github.io/apm/guides/dependencies/"
            )

        # Accept any path ending in a recognised virtual file
        # extension. Reject other dotted final segments so typos like
        # `prompts/file.txt` fail fast instead of silently
        # mis-classifying as a subdirectory.
        if any(virtual_path.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            pass
        else:
            last_segment = virtual_path.split("/")[-1]
            if "." in last_segment:
                raise InvalidVirtualPackageExtensionError(
                    f"Invalid virtual package path '{virtual_path}'. "
                    f"Individual files must end with one of: {', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
                    f"For subdirectory packages, the path should not have a file extension."
                )

    return is_virtual_package, virtual_path, validated_host
