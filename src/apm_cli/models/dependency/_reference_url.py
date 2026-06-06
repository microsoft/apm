"""URL / SSH / shorthand resolution mixin for ``DependencyReference``.

Composed onto :class:`~apm_cli.models.dependency.reference.DependencyReference`
via mixin inheritance; ``cls`` binds to ``DependencyReference`` at call time and
cross-method calls resolve through the MRO. Nothing here imports the composed
class, so the package stays free of import cycles.
"""

import re
import urllib.parse

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import (
    default_host,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    parse_artifactory_path,
    unsupported_host_error,
    validate_ssh_user,
)
from ...utils.path_security import validate_path_segments
from ..validation import InvalidVirtualPackageExtensionError
from ._reference_util import (
    _DEFAULT_SCHEME_PORTS,
    _NON_ADO_PATH_SEGMENT_RE,
    _path_segment_pattern,
)


class _ReferenceUrlMixin:
    """HTTPS/SSH/shorthand URL parsing, normalisation, and validation."""

    @staticmethod
    def _normalize_parent_repo_decl_path(raw: str) -> str:
        """Normalize ``path`` for ``git: parent`` to a single canonical relative path."""
        s = raw.strip().replace("\\", "/").strip()
        s = s.strip("/")
        segments = [seg for seg in s.split("/") if seg]
        if not segments:
            raise ValueError("'path' field must be a non-empty string")
        normalized = "/".join(segments)
        validate_path_segments(normalized, context="path")
        return normalized

    @staticmethod
    def _parse_ssh_url(dependency_str: str):
        """Parse an SCP-shorthand SSH URL (``<user>@host:owner/repo``).

        Accepts any SSH username (not just ``git``), so EMU and custom GHE
        SSH accounts (e.g. ``enterprise-user@ghe.corp.com:org/repo``) parse
        correctly. SCP shorthand cannot carry a port (``:`` is the path
        separator), so the returned port is always ``None``. For custom SSH
        ports, use the ``ssh://`` URL form which is handled by
        ``_parse_ssh_protocol_url``.

        Returns:
            ``(host, port, repo_url, reference, alias)`` or *None* if not an SCP URL.
        """
        ssh_match = SCP_LIKE_RE.match(dependency_str)
        if not ssh_match:
            return None

        user = ssh_match.group("user")
        host = ssh_match.group("host")
        ssh_repo_part = ssh_match.group("path")

        reference = None
        alias = None

        if "@" in ssh_repo_part:
            ssh_repo_part, alias = ssh_repo_part.rsplit("@", 1)
            alias = alias.strip()

        if "#" in ssh_repo_part:
            repo_part, reference = ssh_repo_part.rsplit("#", 1)
            reference = reference.strip()
        else:
            repo_part = ssh_repo_part

        had_git_suffix = repo_part.endswith(".git")
        if had_git_suffix:
            repo_part = repo_part[:-4]

        repo_url = repo_part.strip()

        # SCP syntax (git@host:path) uses ':' as the path separator, so it
        # cannot carry a port.  Detect when the first segment is a valid TCP
        # port number (1-65535) and raise an actionable error instead of
        # silently misparsing the port as part of the repo path.
        segments = repo_url.split("/", 1)
        first_segment = segments[0]
        if re.fullmatch(r"[0-9]+", first_segment):
            port_candidate = int(first_segment)
            if 1 <= port_candidate <= 65535:
                remaining_path = segments[1] if len(segments) > 1 else ""
                if remaining_path:
                    git_suffix = ".git" if had_git_suffix else ""
                    ref_suffix = f"#{reference}" if reference else ""
                    alias_suffix = f"@{alias}" if alias else ""
                    suggested = f"ssh://{user}@{host}:{port_candidate}/{remaining_path}{git_suffix}{ref_suffix}{alias_suffix}"
                    raise ValueError(
                        f"It looks like '{first_segment}' in '{user}@{host}:{repo_url}' "
                        f"is a port number, but SCP-style URLs (<user>@host:path) cannot "
                        f"carry a port. Use the ssh:// URL form instead:\n"
                        f"  {suggested}"
                    )
                else:
                    raise ValueError(
                        f"It looks like '{first_segment}' in '{user}@{host}:{first_segment}' "
                        f"is a port number, but no repository path follows it. "
                        f"SCP-style URLs (<user>@host:path) cannot carry a port. "
                        f"Use the ssh:// URL form: ssh://{user}@{host}:{port_candidate}/<owner>/<repo>.git"
                    )

        # Security: reject traversal sequences in SSH repo paths
        validate_path_segments(repo_url, context="SSH repository path", reject_empty=True)

        ssh_user = validate_ssh_user(user)
        return host, None, repo_url, reference, alias, ssh_user

    @classmethod
    def _resolve_virtual_shorthand_repo(cls, repo_url, validated_host, virtual_path=None):
        """Narrow a virtual-package shorthand to just the base repo path.

        When a virtual package is given without a URL scheme
        (e.g. ``github.com/owner/repo/path/file.prompt.md``), this strips
        the virtual suffix so the downstream shorthand resolver only sees
        the ``owner/repo`` (or ``org/project/repo`` for ADO) portion.

        Returns:
            ``(host, repo_url)`` where *host* may be ``None``.
        """
        parts = repo_url.split("/")

        if "_git" in parts:
            git_idx = parts.index("_git")
            parts = parts[:git_idx] + parts[git_idx + 1 :]

        host = None
        if len(parts) >= 3 and is_supported_git_host(parts[0]):
            host = parts[0]
            if is_azure_devops_hostname(parts[0]):
                if is_visualstudio_legacy_hostname(parts[0]):
                    # myorg.visualstudio.com/proj/repo/path: org in subdomain,
                    # need at least host + proj + repo + 1 virtual segment.
                    if len(parts) < 4:
                        raise ValueError(
                            "Invalid Azure DevOps virtual package format: must be "
                            "myorg.visualstudio.com/project/repo/path"
                        )
                    repo_url = "/".join(parts[1:3])
                else:
                    # dev.azure.com/org/proj/repo/path: org in path
                    if len(parts) < 5:
                        raise ValueError(
                            "Invalid Azure DevOps virtual package format: must be dev.azure.com/org/project/repo/path"
                        )
                    repo_url = "/".join(parts[1:4])
            elif is_artifactory_path(parts[1:]):
                art_result = parse_artifactory_path(parts[1:])
                if art_result:
                    repo_url = f"{art_result[1]}/{art_result[2]}"
            elif is_gitlab_hostname(parts[0]) and virtual_path:
                vparts = [p for p in virtual_path.split("/") if p]
                tail = len(vparts)
                if tail > 0 and len(parts) > 1 + tail:
                    repo_url = "/".join(parts[1 : len(parts) - tail])
                else:
                    repo_url = "/".join(parts[1:])
            else:
                repo_url = "/".join(parts[1:3])
        elif len(parts) >= 2:
            if not host:
                host = default_host()
            if validated_host and is_azure_devops_hostname(validated_host):
                if len(parts) < 4:
                    raise ValueError(
                        "Invalid Azure DevOps virtual package format: expected at least org/project/repo/path"
                    )
                repo_url = "/".join(parts[:3])
            elif validated_host is None and virtual_path:
                # Bare shorthand under registry-only mode may carry a nested
                # repo path (GitLab subgroup via proxy).  Trust the boundary
                # already chosen by ``_bare_shorthand_repo_segment_count`` --
                # everything before the virtual tail belongs to the repo.
                vparts = [p for p in virtual_path.split("/") if p]
                tail = len(vparts)
                if tail > 0 and len(parts) > tail + 1:
                    repo_url = "/".join(parts[: len(parts) - tail])
                else:
                    repo_url = "/".join(parts[:2])
            else:
                repo_url = "/".join(parts[:2])

        return host, repo_url

    @classmethod
    def _resolve_shorthand_to_parsed_url(cls, repo_url, host):
        """Resolve a non-URL shorthand path into a ``urllib``-parsed URL.

        Handles ``user/repo``, ``github.com/user/repo``,
        ``dev.azure.com/org/project/repo``, and Artifactory VCS paths.
        Validates path components before returning.

        Returns:
            ``(parsed_url, host)``
        """
        parts = repo_url.split("/")

        if "_git" in parts:
            git_idx = parts.index("_git")
            parts = parts[:git_idx] + parts[git_idx + 1 :]

        if len(parts) >= 3 and is_supported_git_host(parts[0]):
            host = parts[0]
            if is_visualstudio_legacy_hostname(host) and len(parts) >= 3:
                # *.visualstudio.com/proj/repo: org is in the subdomain, path is proj/repo only
                user_repo = "/".join(parts[1:3])
            elif is_azure_devops_hostname(host) and len(parts) >= 4:
                # dev.azure.com/org/proj/repo: org is the first path segment
                user_repo = "/".join(parts[1:4])
            elif not is_github_hostname(host) and not is_azure_devops_hostname(host):
                if is_artifactory_path(parts[1:]):
                    art_result = parse_artifactory_path(parts[1:])
                    if art_result:
                        user_repo = f"{art_result[1]}/{art_result[2]}"
                    else:
                        user_repo = "/".join(parts[1:])
                else:
                    user_repo = "/".join(parts[1:])
            else:
                user_repo = "/".join(parts[1:])
        elif len(parts) >= 2 and "." not in parts[0]:
            if not host:
                host = default_host()
            if is_azure_devops_hostname(host) and len(parts) >= 3:
                user_repo = "/".join(parts[:3])
            elif host and not is_github_hostname(host) and not is_azure_devops_hostname(host):
                user_repo = "/".join(parts)
            elif len(parts) >= 3 and cls._bare_shorthand_repo_segment_count(parts) > 2:
                # Registry-only mode allows nested-group repo paths
                # (GitLab via proxy).  Keep the full multi-segment path.
                user_repo = "/".join(parts[: cls._bare_shorthand_repo_segment_count(parts)])
            else:
                user_repo = "/".join(parts[:2])
        else:
            raise ValueError(
                "Use 'user/repo' or 'github.com/user/repo' or 'dev.azure.com/org/project/repo' format"
            )

        if not user_repo or "/" not in user_repo:
            raise ValueError(
                f"Invalid repository format: {repo_url}. Expected 'user/repo' or 'org/project/repo'"
            )

        uparts = user_repo.split("/")
        is_ado_host = host and is_azure_devops_hostname(host)

        if is_ado_host:
            # *.visualstudio.com encodes org in subdomain -> proj/repo is sufficient (2 parts).
            # dev.azure.com encodes org in path -> org/proj/repo required (3 parts).
            min_ado_parts = 2 if is_visualstudio_legacy_hostname(host) else 3
            if len(uparts) < min_ado_parts:
                raise ValueError(
                    f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
                )
        elif len(uparts) < 2:
            raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")

        allowed_pattern = _path_segment_pattern(is_ado_host)
        validate_path_segments("/".join(uparts), context="repository path")
        for part in uparts:
            if not re.match(allowed_pattern, part.rstrip(".git")):
                raise ValueError(f"Invalid repository path component: {part}")

        quoted_repo = "/".join(urllib.parse.quote(p, safe="") for p in uparts)
        github_url = urllib.parse.urljoin(f"https://{host}/", quoted_repo)
        parsed_url = urllib.parse.urlparse(github_url)

        return parsed_url, host

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

        if is_ado_host:
            return cls._validate_ado_url_repo_path(hostname, path, path_parts)

        if len(path_parts) < 2:
            raise ValueError(
                f"Invalid repository path: expected at least 'user/repo', got '{path}'"
            )
        # Strip the Artifactory VCS prefix so ``repo_url`` is the bare
        # ``owner/repo`` -- otherwise URL round-trip through
        # ``to_github_url`` -> ``parse`` would carry the prefix in the
        # repo_url and the orchestrator would double-prefix download URLs.
        # The prefix itself is recovered separately via
        # :meth:`_extract_artifactory_prefix`.
        if is_artifactory_path(path_parts):
            path_parts = path_parts[2:]
        for pp in path_parts:
            if any(pp.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
                raise ValueError(
                    f"Invalid repository path: '{path}' contains a virtual file extension. "
                    f"Use the dict format with 'path:' for virtual packages in HTTPS URLs"
                )

        cls._validate_repo_path_segments(path_parts, is_ado_host=False)
        return "/".join(path_parts), None

    @classmethod
    def _validate_ado_url_repo_path(
        cls, hostname: str, path: str, path_parts: list[str]
    ) -> tuple[str, str | None]:
        """Validate an Azure DevOps URL path, splitting off any virtual sub-path.

        Returns ``(repo_url, virtual_path)`` with the org injected from the
        subdomain for ``*.visualstudio.com`` so the result is always
        ``org/project/repo``.
        """
        # *.visualstudio.com encodes org in the subdomain; URL path is proj/repo (2 parts).
        # dev.azure.com encodes org as the first path segment; URL path is org/proj/repo (3 parts).
        is_vs_legacy = is_visualstudio_legacy_hostname(hostname)
        min_ado_parts = 2 if is_vs_legacy else 3
        if len(path_parts) < min_ado_parts:
            raise ValueError(
                f"Invalid Azure DevOps repository path: expected 'org/project/repo', got '{path}'"
            )

        url_virtual_path: str | None = None
        if len(path_parts) > min_ado_parts:
            # Extra segments are a virtual sub-path (e.g. sub/path in
            # https://dev.azure.com/org/proj/_git/repo/sub/path or
            # https://myorg.visualstudio.com/proj/_git/repo/sub/path).
            ado_virtual = "/".join(path_parts[min_ado_parts:])
            cls._validate_ado_virtual_suffix(ado_virtual)
            url_virtual_path = ado_virtual
            path_parts = path_parts[:min_ado_parts]

        # For *.visualstudio.com, inject the org from the subdomain so that the
        # normalised repo_url is always org/project/repo (matching dev.azure.com).
        if is_vs_legacy:
            vs_org = hostname.split(".")[0]
            path_parts = [vs_org, *path_parts]

        cls._validate_repo_path_segments(path_parts, is_ado_host=True)
        return "/".join(path_parts), url_virtual_path

    @classmethod
    def _validate_ado_virtual_suffix(cls, ado_virtual: str) -> None:
        """Validate an ADO URL virtual sub-path (traversal + extension shape)."""
        # Security: reject path traversal in virtual path.
        validate_path_segments(ado_virtual, context="virtual path")

        # Reject removed .collection.yml extensions.
        if any(ado_virtual.endswith(ext) for ext in cls.REMOVED_COLLECTION_EXTENSIONS):
            raise ValueError(
                f".collection.yml is no longer supported. "
                f"Convert '{ado_virtual}' to an apm.yml with a "
                f"'dependencies' section. "
                f"See: https://microsoft.github.io/apm/guides/dependencies/"
            )

        # Accept any recognised virtual file extension; reject other
        # dotted final segments (mirrors shorthand virtual detection).
        if any(ado_virtual.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            return
        last_segment = ado_virtual.split("/")[-1]
        if "." in last_segment:
            raise InvalidVirtualPackageExtensionError(
                f"Invalid virtual package path '{ado_virtual}'. "
                f"Individual files must end with one of: "
                f"{', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
                f"For subdirectory packages, the path should not have a file extension."
            )

    @staticmethod
    def _validate_repo_path_segments(path_parts: list[str], *, is_ado_host: bool) -> None:
        """Validate each repo path segment against the host's allowed char set."""
        allowed_pattern = _path_segment_pattern(is_ado_host)
        validate_path_segments(
            "/".join(path_parts),
            context="repository URL path",
            reject_empty=True,
        )
        for part in path_parts:
            if not re.match(allowed_pattern, part):
                raise ValueError(f"Invalid repository path component: {part}")

    @classmethod
    def _parse_standard_url(
        cls,
        dependency_str: str,
        is_virtual_package: bool,
        virtual_path: str | None,
        validated_host: str | None,
    ) -> tuple[str, int | None, str, str | None, str | None, bool, str | None]:
        """Parse a non-SSH dependency string (HTTPS, FQDN, or shorthand).

        Detects scheme vs shorthand, delegates host-specific resolution to
        helpers, then validates the resulting URL path.

        Returns:
            ``(host, port, repo_url, reference, alias, effective_is_virtual,
            effective_virtual_path)`` -- the last two reflect any ADO sub-path
            segments embedded in the URL itself (issue #1128).
        """
        host = None
        port = None
        alias = None

        reference = None
        if "#" in dependency_str:
            repo_part, reference = dependency_str.rsplit("#", 1)
            reference = reference.strip()
        else:
            repo_part = dependency_str

        repo_url = repo_part.strip()

        # Lowercase copy for scheme detection -- kept from the original
        # repo_url so the URL-vs-shorthand check below still works after
        # the virtual shorthand resolver has narrowed repo_url.
        repo_url_lower = repo_url.lower()

        # For virtual packages without a URL scheme, narrow to just owner/repo
        if is_virtual_package and not repo_url_lower.startswith(("https://", "http://")):
            host, repo_url = cls._resolve_virtual_shorthand_repo(
                repo_url, validated_host, virtual_path
            )

        # Normalize to URL format for secure parsing
        if repo_url_lower.startswith(("https://", "http://")):
            parsed_url = urllib.parse.urlparse(repo_url)
            host = parsed_url.hostname or ""
            port = parsed_url.port  # capture :PORT from https://host:8443/...
            # Normalise default-scheme ports (443 for HTTPS, 80 for HTTP)
            # so lockfile keys are consistent regardless of URL spelling.
            scheme = (parsed_url.scheme or "").lower()
            if port == _DEFAULT_SCHEME_PORTS.get(scheme):
                port = None
        else:
            parsed_url, host = cls._resolve_shorthand_to_parsed_url(repo_url, host)

        repo_url, url_virtual_path = cls._validate_url_repo_path(parsed_url)

        # If URL contained extra ADO sub-path segments, they become the virtual
        # path (overriding the _detect_virtual_package result which returns
        # early for https:// URLs).
        effective_is_virtual = is_virtual_package
        effective_virtual_path = virtual_path
        if url_virtual_path is not None:
            effective_is_virtual = True
            effective_virtual_path = url_virtual_path

        if not host:
            host = default_host()

        return host, port, repo_url, reference, alias, effective_is_virtual, effective_virtual_path

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
        if not all(re.match(_NON_ADO_PATH_SEGMENT_RE, s) for s in segments):
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
