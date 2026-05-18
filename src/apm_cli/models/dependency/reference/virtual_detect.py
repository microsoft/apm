"""DependencyReference model  -- core dependency representation and parsing."""

import urllib.parse
from dataclasses import dataclass

from ....utils.github_host import (
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    unsupported_host_error,
)
from ....utils.path_security import (
    PathTraversalError,
    validate_path_segments,
)
from ...validation import InvalidVirtualPackageExtensionError

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


@dataclass(frozen=True, slots=True)
class _HostTypeFlags:
    """Flags describing the detected host type for virtual package detection."""

    is_ado: bool
    is_artifactory: bool
    is_generic_host: bool
    is_gitlab_host: bool


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


def _parse_host_from_first_segment(check_str: str) -> tuple[str | None, str]:
    """Parse and validate host from first path segment.

    Returns:
        (validated_host, remaining_check_str)
    """
    if "/" not in check_str:
        return None, check_str

    first_segment = check_str.split("/")[0]
    if "." not in first_segment:
        if check_str.startswith("gh/"):
            return None, "/".join(check_str.split("/")[1:])
        return None, check_str

    test_url = f"https://{check_str}"
    try:
        parsed = urllib.parse.urlparse(test_url)
        hostname = parsed.hostname

        if hostname and is_supported_git_host(hostname):
            path_parts = parsed.path.lstrip("/").split("/")
            if len(path_parts) >= 2:
                return hostname, "/".join(check_str.split("/")[1:])
        else:
            raise ValueError(unsupported_host_error(hostname or first_segment))
    except (ValueError, AttributeError) as e:
        if isinstance(e, ValueError) and "Invalid Git host" in str(e):
            raise
        raise ValueError(unsupported_host_error(first_segment)) from e

    return None, check_str


def _validate_virtual_path_extension_rules(virtual_path: str, cls) -> None:
    """Validate virtual path extension rules."""
    if any(virtual_path.endswith(ext) for ext in cls.REMOVED_COLLECTION_EXTENSIONS):
        raise ValueError(
            f".collection.yml is no longer supported. "
            f"Convert '{virtual_path}' to an apm.yml with a "
            f"'dependencies' section. "
            f"See: https://microsoft.github.io/apm/guides/dependencies/"
        )

    if any(virtual_path.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
        return

    last_segment = virtual_path.split("/")[-1]
    if "." in last_segment:
        raise InvalidVirtualPackageExtensionError(
            f"Invalid virtual package path '{virtual_path}'. "
            f"Individual files must end with one of: {', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
            f"For subdirectory packages, the path should not have a file extension."
        )


def _compute_min_base_helper(
    path_segments: list,
    validated_host: str | None,
    flags: _HostTypeFlags,
    cls,
) -> int:
    """Helper that computes minimum base segments. Called by @classmethod wrapper."""
    min_base_segments = 2
    if flags.is_ado:
        min_base_segments = (
            2 if validated_host and is_visualstudio_legacy_hostname(validated_host) else 3
        )
    elif flags.is_artifactory:
        min_base_segments = 4
    elif flags.is_generic_host:
        has_virtual_ext = any(
            any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS) for seg in path_segments
        )
        has_collection = "collections" in path_segments
        if flags.is_gitlab_host:
            min_base_segments = cls._gitlab_shorthand_repo_segment_count(
                path_segments, has_virtual_ext, has_collection
            )
        elif not (has_virtual_ext or has_collection):
            min_base_segments = len(path_segments)
    return min_base_segments


@classmethod
def _compute_min_base_segments(
    cls,
    path_segments: list,
    validated_host: str | None,
    flags: _HostTypeFlags,
) -> int:
    """Return the minimum path-segment count that forms the repo base address."""
    return _compute_min_base_helper(path_segments, validated_host, flags, cls)


@classmethod
def _detect_virtual_package(cls, dependency_str: str):
    """Detect whether *dependency_str* refers to a virtual package.

    Returns:
        (is_virtual_package, virtual_path, validated_host)
    """
    temp_str = dependency_str
    if "#" in temp_str:
        temp_str = temp_str.rsplit("#", 1)[0]

    is_virtual_package = False
    virtual_path = None
    validated_host = None

    if temp_str.lower().startswith(("git@", "https://", "http://", "ssh://")):
        return is_virtual_package, virtual_path, validated_host

    validated_host, check_str = _parse_host_from_first_segment(temp_str)
    path_segments = [seg for seg in check_str.split("/") if seg]

    flags = _HostTypeFlags(
        is_ado=validated_host is not None and is_azure_devops_hostname(validated_host),
        is_generic_host=(
            validated_host is not None
            and not is_github_hostname(validated_host)
            and not is_azure_devops_hostname(validated_host)
        ),
        is_gitlab_host=validated_host is not None and is_gitlab_hostname(validated_host),
        is_artifactory=False,
    )

    if flags.is_ado and "_git" in path_segments:
        git_idx = path_segments.index("_git")
        path_segments = path_segments[:git_idx] + path_segments[git_idx + 1 :]

    if flags.is_generic_host:
        flags = _HostTypeFlags(
            is_ado=flags.is_ado,
            is_generic_host=flags.is_generic_host,
            is_gitlab_host=flags.is_gitlab_host,
            is_artifactory=is_artifactory_path(path_segments),
        )

    min_base_segments = _compute_min_base_helper(path_segments, validated_host, flags, cls)
    min_virtual_segments = min_base_segments + 1

    if len(path_segments) >= min_virtual_segments:
        is_virtual_package = True
        virtual_path = "/".join(path_segments[min_base_segments:])
        validate_path_segments(virtual_path, context="virtual path")
        _validate_virtual_path_extension_rules(virtual_path, cls)

    return is_virtual_package, virtual_path, validated_host
