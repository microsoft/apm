"""Host-qualified virtual-package shorthand parsing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...utils.github_host import (
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_visualstudio_legacy_hostname,
)
from ...utils.path_security import validate_path_segments
from .identity import _split_shorthand_host_port

_HOST_QUALIFIED_VIRTUAL_ROOTS: frozenset[str] = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".cursor",
        ".gemini",
        ".github",
        ".kiro",
        ".windsurf",
    }
)
_UNCONFIGURED_PLATFORM_PACKAGE_ROOTS: frozenset[str] = frozenset(
    {
        "agents",
        "collections",
        "contexts",
        "instructions",
        "memory",
        "prompts",
        "rules",
        "skills",
    }
)


@dataclass(frozen=True)
class HostQualifiedVirtualShorthand:
    """Parsed host-qualified virtual-package shorthand."""

    host: str
    repo_url: str
    virtual_path: str


class UnsupportedHostQualifiedVirtualPackageError(ValueError):
    """Raised when a host-qualified package path uses an unconfigured host."""

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(
            f"Unsupported package host: '{host}'.\n"
            "APM cannot infer the repository boundary for a host-qualified "
            "package path unless the host is configured or recognized as "
            "GitHub, Azure DevOps, or GitLab.\n"
            f"Configure '{host}' with GITHUB_HOST, GITLAB_HOST/APM_GITLAB_HOSTS, "
            "or ADO_HOST/APM_ADO_HOSTS, then re-run. Alternatively, use an "
            "explicit apm.yml object with 'git:' for the repository URL and "
            "'path:' for the package subpath.\n"
            f"GitLab example: GITLAB_HOST={host} or APM_GITLAB_HOSTS={host}."
        )


def parse_host_qualified_virtual_shorthand(
    dependency_str: str,
    *,
    virtual_file_extensions: tuple[str, ...],
    gitlab_repo_segment_count: Callable[[list[str], bool, bool], int],
) -> HostQualifiedVirtualShorthand | None:
    """Parse ``host/repo/path`` virtual-package shorthand without fallback."""
    source = dependency_str.strip()
    if "#" in source:
        source = source.rsplit("#", 1)[0].strip()
    if source.startswith(("git@", "https://", "http://", "ssh://", "//")):
        return None
    if "/" not in source:
        return None

    parts = [part for part in source.split("/") if part]
    if len(parts) < 4:
        return None
    try:
        host, _port = _split_shorthand_host_port(parts[0])
    except ValueError:
        return None
    if "." not in host:
        return None

    path_segments = parts[1:]
    virtual_start = _host_qualified_virtual_path_start(
        path_segments,
        virtual_file_extensions,
    )
    has_collection = "collections" in path_segments
    has_virtual_ext = any(
        any(segment.endswith(ext) for ext in virtual_file_extensions) for segment in path_segments
    )
    unconfigured_platform_virtual = _unconfigured_platform_host_has_virtual_package_shape(
        host,
        path_segments,
        virtual_start,
        has_collection,
        has_virtual_ext,
    )
    if (
        virtual_start is None
        and not has_collection
        and not has_virtual_ext
        and not unconfigured_platform_virtual
    ):
        return None

    if is_azure_devops_hostname(host):
        ado_segments = [segment for segment in path_segments if segment != "_git"]
        repo_count = 2 if is_visualstudio_legacy_hostname(host) else 3
        if len(ado_segments) <= repo_count:
            return None
        repo_url = "/".join(ado_segments[:repo_count])
        virtual_path = "/".join(ado_segments[repo_count:])
    elif is_github_hostname(host):
        if len(path_segments) <= 2:
            return None
        repo_url = "/".join(path_segments[:2])
        virtual_path = "/".join(path_segments[2:])
    elif is_gitlab_hostname(host):
        if (
            virtual_start is not None
            and path_segments[virtual_start] in _HOST_QUALIFIED_VIRTUAL_ROOTS
        ):
            repo_count = virtual_start
        else:
            repo_count = gitlab_repo_segment_count(
                path_segments,
                has_virtual_ext,
                has_collection,
            )
        if len(path_segments) <= repo_count:
            return None
        repo_url = "/".join(path_segments[:repo_count])
        virtual_path = "/".join(path_segments[repo_count:])
    elif unconfigured_platform_virtual and _looks_like_unconfigured_platform_host(host):
        raise UnsupportedHostQualifiedVirtualPackageError(host)
    else:
        return None

    validate_path_segments(virtual_path, context="virtual path")
    return HostQualifiedVirtualShorthand(host=host, repo_url=repo_url, virtual_path=virtual_path)


def _host_qualified_virtual_path_start(
    path_segments: list[str],
    virtual_file_extensions: tuple[str, ...],
) -> int | None:
    """Return the first segment index that proves a virtual package path."""
    for idx, segment in enumerate(path_segments):
        if idx >= 2 and segment in _HOST_QUALIFIED_VIRTUAL_ROOTS:
            return idx
    for idx, segment in enumerate(path_segments):
        if idx >= 2 and any(segment.endswith(ext) for ext in virtual_file_extensions):
            return idx
    return None


def _looks_like_unconfigured_platform_host(host: str) -> bool:
    """Return whether *host* names a platform that needs explicit config."""
    labels = host.lower().split(".")
    return any(label in {"ado", "azure", "ghe", "github", "gitlab"} for label in labels)


def _unconfigured_platform_host_has_virtual_package_shape(
    host: str,
    path_segments: list[str],
    virtual_start: int | None,
    has_collection: bool,
    has_virtual_ext: bool,
) -> bool:
    """Return whether an unconfigured platform host appears to carry a package path."""
    if virtual_start is not None or has_collection or has_virtual_ext:
        return True
    if any(segment in _UNCONFIGURED_PLATFORM_PACKAGE_ROOTS for segment in path_segments[2:]):
        return True

    labels = set(host.lower().split("."))
    if labels & {"ghe", "github"}:
        return len(path_segments) > 2
    if labels & {"ado", "azure"}:
        ado_segments = [segment for segment in path_segments if segment != "_git"]
        return len(ado_segments) > 3
    return False
