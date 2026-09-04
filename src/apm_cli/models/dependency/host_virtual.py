"""Host-qualified virtual-package shorthand parsing."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from ...utils.github_host import (
    format_github_gitlab_host_conflict_error,
    has_github_gitlab_host_env_conflict,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    parse_artifactory_path,
)
from ...utils.path_security import parse_url_path_segments, validate_path_segments
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


@dataclass(frozen=True)
class HostQualifiedReference:
    """Canonical host/owner/repo/subpath parse for host-qualified references."""

    host: str
    repo_url: str
    subpath: str | None = None
    port: int | None = None

    @property
    def owner(self) -> str | None:
        return repository_owner(self.repo_url)

    @property
    def repo(self) -> str | None:
        segments = _repo_segments(self.repo_url)
        return segments[-1] if segments else None


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
    parsed = parse_host_qualified_reference(
        dependency_str,
        virtual_file_extensions=virtual_file_extensions,
        gitlab_repo_segment_count=gitlab_repo_segment_count,
        virtual_only=True,
    )
    if parsed is None or parsed.subpath is None:
        return None
    return HostQualifiedVirtualShorthand(
        host=parsed.host,
        repo_url=parsed.repo_url,
        virtual_path=parsed.subpath,
    )


def parse_host_qualified_reference(
    dependency_str: str,
    *,
    virtual_file_extensions: tuple[str, ...],
    gitlab_repo_segment_count: Callable[[list[str], bool, bool], int],
    virtual_only: bool = False,
) -> HostQualifiedReference | None:
    """Parse a host-qualified shorthand into canonical host/repo/subpath parts.

    This is the single owner for shorthand strings whose first path token is a
    host. It returns ``None`` for owner/repo shorthand, local paths, and explicit
    URL/SSH forms owned by the URL parser.
    """
    source_parts = _split_host_qualified_source(dependency_str)
    if source_parts is None:
        return None
    host, port, path_segments, source_part_count = source_parts
    if source_part_count >= 4 and has_github_gitlab_host_env_conflict(host):
        raise ValueError(format_github_gitlab_host_conflict_error(host))
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
        virtual_only
        and virtual_start is None
        and not has_collection
        and not has_virtual_ext
        and not unconfigured_platform_virtual
    ):
        return None

    repo_url: str
    virtual_path: str | None = None
    if is_azure_devops_hostname(host):
        had_git_marker = "_git" in path_segments
        ado_segments = _decode_ado_shorthand_segments(
            [segment for segment in path_segments if segment != "_git"]
        )
        repo_count = (
            2
            if is_visualstudio_legacy_hostname(host) and (had_git_marker or len(ado_segments) == 2)
            else 3
        )
        if len(ado_segments) < repo_count:
            return None
        repo_url = "/".join(ado_segments[:repo_count])
        virtual_path = "/".join(ado_segments[repo_count:]) or None
    elif is_github_hostname(host):
        repo_url = "/".join(path_segments[:2])
        virtual_path = "/".join(path_segments[2:]) or None
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
        if len(path_segments) < repo_count:
            return None
        repo_url = "/".join(path_segments[:repo_count])
        virtual_path = "/".join(path_segments[repo_count:]) or None
    elif unconfigured_platform_virtual and _looks_like_unconfigured_platform_host(host):
        raise UnsupportedHostQualifiedVirtualPackageError(host)
    elif is_artifactory_path(path_segments):
        art_result = parse_artifactory_path(path_segments)
        if art_result is None:
            return None
        repo_url = f"{art_result[1]}/{art_result[2]}"
        virtual_path = art_result[3]
    elif has_virtual_ext or has_collection:
        repo_url = "/".join(path_segments[:2])
        virtual_path = "/".join(path_segments[2:]) or None
    else:
        repo_url = "/".join(path_segments)

    if virtual_only and virtual_path is None:
        return None
    if virtual_path is not None:
        validate_path_segments(virtual_path, context="virtual path")
    return HostQualifiedReference(host=host, port=port, repo_url=repo_url, subpath=virtual_path)


def _split_host_qualified_source(
    dependency_str: str,
) -> tuple[str, int | None, list[str], int] | None:
    source = dependency_str.strip()
    if "#" in source:
        source = source.rsplit("#", 1)[0].strip()
    if source.startswith(("git@", "https://", "http://", "ssh://", "//")):
        return None
    if "/" not in source:
        return None
    parts = [part for part in source.split("/") if part]
    if len(parts) < 3:
        return None
    try:
        host, port = _split_shorthand_host_port(parts[0])
    except ValueError:
        return None
    if "." not in host or not is_supported_git_host(host):
        return None
    path_segments = parts[1:]
    if len(path_segments) < 2:
        return None
    return host, port, path_segments, len(parts)


def reject_bare_fqdn_github_gitlab_conflict(raw: str) -> None:
    """Reject shorthand whose first token is configured as both GHES and GitLab."""
    source = raw.strip()
    if "#" in source:
        source = source.rsplit("#", 1)[0].strip()
    if source.startswith(("git@", "https://", "http://", "ssh://", "//")):
        return
    if "/" not in source:
        return
    parts = [part for part in source.split("/") if part]
    if len(parts) < 4:
        return
    host_token = parts[0]
    if "." not in host_token:
        return
    try:
        host, _port = _split_shorthand_host_port(host_token)
    except ValueError:
        return
    if not is_supported_git_host(host):
        return
    if has_github_gitlab_host_env_conflict(host):
        raise ValueError(format_github_gitlab_host_conflict_error(host))


def repository_owner(repo_url: str | None) -> str | None:
    """Return the credential-scope owner/org from a canonical repo path."""
    segments = _repo_segments(repo_url)
    return segments[0] if segments else None


def repository_path_segments(repo_url: str | None) -> list[str]:
    """Return normalized repository path segments."""
    return _repo_segments(repo_url)


def repository_owner_and_repo(repo_url: str | None) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` coordinates from a canonical repository path."""
    segments = _repo_segments(repo_url)
    if len(segments) < 2:
        return None
    return segments[0], "/".join(segments[1:])


def dependency_repository_owner(dep_ref: object) -> str | None:
    """Return the owner/org to use for a parsed dependency reference."""
    return repository_owner(getattr(dep_ref, "repo_url", None))


def repository_owner_from_reference_text(source: str | None) -> str | None:
    """Return the owner/org encoded in a raw reference or repo path string."""
    if not source:
        return None
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme and parsed.netloc:
        segments = [segment for segment in parsed.path.split("/") if segment and segment != "_git"]
        return segments[0] if segments else None
    return repository_owner(source)


def _repo_segments(repo_url: str | None) -> list[str]:
    if not repo_url:
        return []
    return [segment for segment in repo_url.split("/") if segment]


def _decode_ado_shorthand_segments(segments: list[str]) -> list[str]:
    decoded: list[str] = []
    for segment in segments:
        if "%" not in segment:
            decoded.append(segment)
            continue
        _raw, decoded_segment = parse_url_path_segments(
            segment,
            context="Azure DevOps repository path",
        )
        decoded.append(decoded_segment[0])
    return decoded


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
    if not labels:
        return False
    if labels[0] == "github" and len(labels) > 1 and labels[1] == "com":
        return False
    return labels[0] in {"ado", "azure", "ghe", "github", "gitlab"}


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
