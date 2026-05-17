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


@staticmethod
def _parse_ssh_protocol_url(url: str):
    """Parse an ``ssh://`` protocol URL using ``urllib.parse.urlparse``.

    Unlike SCP shorthand (``git@host:path``), the ``ssh://`` form is a real
    URL that can carry a port. Parsing it via ``urlparse`` preserves the
    port and cleanly separates the fragment (``#ref``) from the path, so
    APM-specific ``@alias`` suffixes are handled without regex gymnastics.

    Supported forms:
        ssh://git@host/owner/repo.git
        ssh://git@host:7999/owner/repo.git
        ssh://git@host/owner/repo.git#ref
        ssh://git@host:7999/owner/repo.git#ref@alias
        ssh://git@host/owner/repo.git@alias

    Returns:
        ``(host, port, repo_url, reference, alias)`` or ``None`` if the
        input is not an ``ssh://`` URL.
    """
    if not url.startswith("ssh://"):
        return None

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port  # int or None
    # Normalise default SSH port so ssh://host:22/... matches ssh://host/...
    if port == _DEFAULT_SCHEME_PORTS.get("ssh"):
        port = None
    path = parsed.path.lstrip("/")
    fragment = parsed.fragment

    reference: str | None = None
    alias: str | None = None

    # Fragment holds "ref" or "ref@alias"
    if fragment:
        if "@" in fragment:
            ref_part, alias_part = fragment.rsplit("@", 1)
            reference = ref_part.strip() or None
            alias = alias_part.strip() or None
        else:
            reference = fragment.strip() or None

    # Bare "@alias" (no #ref) still lives on the path
    if alias is None and "@" in path:
        path, alias_part = path.rsplit("@", 1)
        alias = alias_part.strip() or None

    if path.endswith(".git"):
        path = path[:-4]

    repo_url = path.strip()

    # Security: reject traversal sequences in SSH repo paths
    validate_path_segments(repo_url, context="SSH repository path", reject_empty=True)

    return host, port, repo_url, reference, alias


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


@classmethod
def parse_from_dict(cls, entry: dict) -> "DependencyReference":
    """Parse an object-style dependency entry from apm.yml.

    Supports the Cargo-inspired object format:

        - git: https://gitlab.com/acme/coding-standards.git
          path: instructions/security
          ref: v2.0

        - git: git@bitbucket.org:team/rules.git
          path: prompts/review.prompt.md

    Also supports local path entries:

        - path: ./packages/my-shared-skills

    Args:
        entry: Dictionary with 'git' or 'path' (required), plus optional fields

    Returns:
        DependencyReference: Parsed dependency reference

    Raises:
        ValueError: If the entry is missing required fields or has invalid format
    """
    # Support dict-form local path: { path: ./local/dir }
    if "path" in entry and "git" not in entry:
        local = entry["path"]
        if not isinstance(local, str) or not local.strip():
            raise ValueError("'path' field must be a non-empty string")
        local = local.strip()
        if not cls.is_local_path(local):
            raise ValueError(
                "Object-style dependency must have a 'git' field, "
                "or 'path' must be a local filesystem path "
                "(starting with './', '../', '/', or '~')"
            )
        return cls.parse(local)

    if "git" not in entry:
        raise ValueError("Object-style dependency must have a 'git' or 'path' field")

    git_url = entry["git"]
    if not isinstance(git_url, str) or not git_url.strip():
        raise ValueError("'git' field must be a non-empty string")

    # Monorepo parent inheritance (literal ``git: parent`` only; resolver expands)
    if git_url == "parent":
        path_raw = entry.get("path")
        if path_raw is None:
            raise ValueError("Object-style dependency with git: 'parent' requires a 'path' field")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise ValueError("'path' field must be a non-empty string")
        normalized_path = cls._normalize_parent_repo_decl_path(path_raw)

        ref_override = entry.get("ref")
        alias_override = entry.get("alias")
        reference: str | None = None
        if ref_override is not None:
            if not isinstance(ref_override, str) or not ref_override.strip():
                raise ValueError("'ref' field must be a non-empty string")
            reference = ref_override.strip()

        alias_val: str | None = None
        if alias_override is not None:
            if not isinstance(alias_override, str) or not alias_override.strip():
                raise ValueError("'alias' field must be a non-empty string")
            alias_override = alias_override.strip()
            if not re.match(r"^[a-zA-Z0-9._-]+$", alias_override):
                raise ValueError(
                    f"Invalid alias: {alias_override}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
                )
            alias_val = alias_override

        return cls(
            repo_url="_parent",
            host=None,
            reference=reference,
            alias=alias_val,
            virtual_path=normalized_path,
            is_virtual=True,
            is_parent_repo_inheritance=True,
        )

    sub_path = entry.get("path")
    ref_override = entry.get("ref")
    alias_override = entry.get("alias")
    allow_insecure = entry.get("allow_insecure", False)
    if not isinstance(allow_insecure, bool):
        raise ValueError("'allow_insecure' field must be a boolean")

    # Validate sub_path if provided
    if sub_path is not None:
        if not isinstance(sub_path, str) or not sub_path.strip():
            raise ValueError("'path' field must be a non-empty string")
        sub_path = sub_path.strip().strip("/")
        # Normalize backslashes to forward slashes for cross-platform safety
        sub_path = sub_path.replace("\\", "/").strip().strip("/")
        # Security: reject path traversal
        validate_path_segments(sub_path, context="path")

    # Parse the git URL using the standard parser
    dep = cls.parse(git_url)
    dep.allow_insecure = allow_insecure

    # Apply overrides from the object fields
    if ref_override is not None:
        if not isinstance(ref_override, str) or not ref_override.strip():
            raise ValueError("'ref' field must be a non-empty string")
        dep.reference = ref_override.strip()

    if alias_override is not None:
        if not isinstance(alias_override, str) or not alias_override.strip():
            raise ValueError("'alias' field must be a non-empty string")
        alias_override = alias_override.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", alias_override):
            raise ValueError(
                f"Invalid alias: {alias_override}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
            )
        dep.alias = alias_override

    # Apply sub-path as virtual package
    if sub_path:
        dep.virtual_path = sub_path
        dep.is_virtual = True

    # Parse skills: field (SKILL_BUNDLE subset selection)
    skills_raw = entry.get("skills")
    if skills_raw is not None:
        if not isinstance(skills_raw, (list,)):
            raise ValueError("'skills' field must be a list of skill names")
        if len(skills_raw) == 0:
            raise ValueError(
                "skills: must contain at least one name; "
                "remove the field to install all skills in the bundle."
            )
        seen: set = set()
        validated: list = []
        for name in skills_raw:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Each entry in 'skills' must be a non-empty string")
            name = name.strip()
            # Path safety: reject traversal sequences
            validate_path_segments(name, context="skills/<name>")
            if name not in seen:
                seen.add(name)
                validated.append(name)
        dep.skill_subset = sorted(validated)

    return dep


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

    return host, None, repo_url, reference, alias


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
        host, repo_url = cls._resolve_virtual_shorthand_repo(repo_url, validated_host, virtual_path)

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
def parse(cls, dependency_str: str) -> "DependencyReference":
    """Parse a dependency string into a DependencyReference.

    Supports formats:
    - user/repo
    - user/repo#branch
    - user/repo#v1.0.0
    - user/repo#commit_sha
    - github.com/user/repo#ref
    - user/repo@alias
    - user/repo#ref@alias
    - user/repo/path/to/file.prompt.md (virtual file package)
    - user/repo/skills/foo (virtual subdirectory package)
    - user/repo/collections/foo (virtual subdirectory package)
    - https://gitlab.com/owner/repo.git (generic HTTPS git URL)
    - git@gitlab.com:owner/repo.git (SSH git URL)
    - ssh://git@gitlab.com/owner/repo.git (SSH protocol URL)

    Ambiguous GitLab nested-group shorthand cannot cover every depth; use
    object form (``git:`` + ``path:`` in ``apm.yml``) as the supported
    escape hatch.

    - ./local/path (local filesystem path)
    - /absolute/path (local filesystem path)
    - ../relative/path (local filesystem path)

    Any valid FQDN is accepted as a git host (GitHub, GitLab, Bitbucket,
    self-hosted instances, etc.).

    Args:
        dependency_str: The dependency string to parse

    Returns:
        DependencyReference: Parsed dependency reference

    Raises:
        ValueError: If the dependency string format is invalid
    """
    if not dependency_str.strip():
        raise ValueError("Empty dependency string")

    dependency_str = urllib.parse.unquote(dependency_str)

    if any(ord(c) < 32 for c in dependency_str):
        raise ValueError("Dependency string contains invalid control characters")

    # --- Local path detection (must run before URL/host parsing) ---
    if cls.is_local_path(dependency_str):
        local = dependency_str.strip()
        pkg_name = Path(local).name
        if not pkg_name or pkg_name in (".", ".."):
            raise ValueError(
                f"Local path '{local}' does not resolve to a named directory. "
                f"Use a path that ends with a directory name "
                f"(e.g., './my-package' instead of './')."
            )
        return cls(
            repo_url=f"_local/{pkg_name}",
            is_local=True,
            local_path=local,
        )

    if dependency_str.startswith("//"):
        raise ValueError(
            unsupported_host_error("//...", context="Protocol-relative URLs are not supported")
        )

    maybe_raise_bare_fqdn_github_gitlab_conflict(dependency_str)

    # Phase 1: detect virtual packages
    is_virtual_package, virtual_path, validated_host = cls._detect_virtual_package(dependency_str)

    # Phase 2: parse SSH (ssh:// URL first -- it preserves port; then SCP
    # shorthand), otherwise fall back to HTTPS/shorthand parsing.
    explicit_scheme: str | None = None
    ssh_proto_result = cls._parse_ssh_protocol_url(dependency_str)
    if ssh_proto_result:
        host, port, repo_url, reference, alias = ssh_proto_result
        explicit_scheme = "ssh"
    else:
        scp_result = cls._parse_ssh_url(dependency_str)
        if scp_result:
            host, port, repo_url, reference, alias = scp_result
            explicit_scheme = "ssh"
        else:
            host, port, repo_url, reference, alias, is_virtual_package, virtual_path = (
                cls._parse_standard_url(
                    dependency_str, is_virtual_package, virtual_path, validated_host
                )
            )
            _stripped = dependency_str.strip().lower()
            if _stripped.startswith("https://"):
                explicit_scheme = "https"
            elif _stripped.startswith("http://"):
                explicit_scheme = "http"

    # Phase 3: final validation and ADO field extraction
    ado_organization, ado_project, ado_repo = cls._validate_final_repo_fields(host, repo_url)

    if alias and not re.match(r"^[a-zA-Z0-9._-]+$", alias):
        raise ValueError(
            f"Invalid alias: {alias}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
        )

    # Extract Artifactory prefix from the original path if applicable
    is_ado_final = host and is_azure_devops_hostname(host)
    artifactory_prefix = None
    if host and not is_ado_final:
        artifactory_prefix = cls._extract_artifactory_prefix(dependency_str, host)

    return cls(
        repo_url=repo_url,
        host=host,
        port=port,
        explicit_scheme=explicit_scheme,
        reference=reference,
        alias=alias,
        virtual_path=virtual_path,
        is_virtual=is_virtual_package,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        artifactory_prefix=artifactory_prefix,
        is_insecure=urllib.parse.urlparse(dependency_str).scheme.lower() == "http",
    )
