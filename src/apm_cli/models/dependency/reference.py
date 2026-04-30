"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.github_host import (
    default_host,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_supported_git_host,
    parse_artifactory_path,
    unsupported_host_error,
)
from ...utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ..validation import InvalidVirtualPackageExtensionError
from .types import VirtualPackageType


@dataclass
class DependencyReference:
    """Represents a reference to an APM dependency."""

    repo_url: str  # e.g., "user/repo" for GitHub or "org/project/repo" for Azure DevOps
    host: Optional[str] = (
        None  # Optional host (github.com, dev.azure.com, or enterprise host)
    )
    port: Optional[int] = None  # Non-standard SSH/HTTPS port (e.g. 7999 for Bitbucket DC)
    explicit_scheme: Optional[str] = (
        None  # User-stated transport: "ssh", "https", "http", or None for shorthand
    )
    reference: Optional[str] = None  # e.g., "main", "v1.0.0", "abc123"
    alias: Optional[str] = None  # Optional alias for the dependency
    virtual_path: Optional[str] = (
        None  # Path for virtual packages (e.g., "prompts/file.prompt.md")
    )
    is_virtual: bool = (
        False  # True if this is a virtual package (individual file or collection)
    )

    # Azure DevOps specific fields (ADO uses org/project/repo structure)
    ado_organization: Optional[str] = None  # e.g., "dmeppiel-org"
    ado_project: Optional[str] = None  # e.g., "market-js-app"
    ado_repo: Optional[str] = None  # e.g., "compliance-rules"

    # Local path dependency fields
    is_local: bool = False  # True if this is a local filesystem dependency
    local_path: Optional[str] = (
        None  # Original local path string (e.g., "./packages/my-pkg")
    )

    artifactory_prefix: Optional[str] = (
        None  # e.g., "artifactory/github" (repo key path)
    )

    # HTTP (insecure) dependency fields
    is_insecure: bool = False  # True when the dependency URL uses http://
    allow_insecure: bool = False  # True if this HTTP dep is explicitly allowed

    # SKILL_BUNDLE subset selection (persisted in apm.yml `skills:` field)
    skill_subset: Optional[List[str]] = None  # Sorted skill names, or None = all

    # Supported file extensions for virtual packages
    VIRTUAL_FILE_EXTENSIONS = (
        ".prompt.md",
        ".instructions.md",
        ".chatmode.md",
        ".agent.md",
    )

    def is_artifactory(self) -> bool:
        """Check if this reference points to a JFrog Artifactory VCS repository."""
        return self.artifactory_prefix is not None

    def is_azure_devops(self) -> bool:
        """Check if this reference points to Azure DevOps."""
        from ...utils.github_host import is_azure_devops_hostname

        return self.host is not None and is_azure_devops_hostname(self.host)

    @property
    def virtual_type(self) -> "Optional[VirtualPackageType]":
        """Return the type of virtual package, or None if not virtual."""
        if not self.is_virtual or not self.virtual_path:
            return None
        if any(self.virtual_path.endswith(ext) for ext in self.VIRTUAL_FILE_EXTENSIONS):
            return VirtualPackageType.FILE
        if "/collections/" in self.virtual_path or self.virtual_path.startswith(
            "collections/"
        ):
            return VirtualPackageType.COLLECTION
        return VirtualPackageType.SUBDIRECTORY

    def is_virtual_file(self) -> bool:
        """Check if this is a virtual file package (individual file)."""
        return self.virtual_type == VirtualPackageType.FILE

    def is_virtual_collection(self) -> bool:
        """Check if this is a virtual collection package."""
        return self.virtual_type == VirtualPackageType.COLLECTION

    def is_virtual_subdirectory(self) -> bool:
        """Check if this is a virtual subdirectory package (e.g., Claude Skill).

        A subdirectory package is a virtual package that:
        - Has a virtual_path that is NOT a file extension we recognize
        - Is NOT a collection (doesn't have /collections/ in path)
        - Is a directory path (likely containing SKILL.md or apm.yml)

        Examples:
            - ComposioHQ/awesome-claude-skills/brand-guidelines -> True
            - owner/repo/prompts/file.prompt.md -> False (is_virtual_file)
            - owner/repo/collections/name -> False (is_virtual_collection)
        """
        return self.virtual_type == VirtualPackageType.SUBDIRECTORY

    def get_virtual_package_name(self) -> str:
        """Generate a package name for this virtual package.

        For virtual packages, we create a sanitized name from the path:
        - owner/repo/prompts/code-review.prompt.md -> repo-code-review
        - owner/repo/collections/project-planning -> repo-project-planning
        - owner/repo/collections/project-planning.collection.yml -> repo-project-planning
        """
        if not self.is_virtual or not self.virtual_path:
            return self.repo_url.split("/")[-1]  # Return repo name as fallback

        # Extract repo name and file/collection name
        repo_parts = self.repo_url.split("/")
        repo_name = repo_parts[-1] if repo_parts else "package"

        # Get the basename without extension
        path_parts = self.virtual_path.split("/")
        if self.is_virtual_collection():
            # For collections: use the collection name without extension
            # collections/project-planning -> project-planning
            # collections/project-planning.collection.yml -> project-planning
            collection_name = path_parts[-1]
            # Strip .collection.yml/.collection.yaml extension if present
            for ext in (".collection.yml", ".collection.yaml"):
                if collection_name.endswith(ext):
                    collection_name = collection_name[: -len(ext)]
                    break
            return f"{repo_name}-{collection_name}"
        else:
            # For individual files: use the filename without extension
            # prompts/code-review.prompt.md -> code-review
            filename = path_parts[-1]
            for ext in self.VIRTUAL_FILE_EXTENSIONS:
                if filename.endswith(ext):
                    filename = filename[: -len(ext)]
                    break
            return f"{repo_name}-{filename}"

    @staticmethod
    def is_local_path(dep_str: str) -> bool:
        """Check if a dependency string looks like a local filesystem path.
        
        Local paths start with './', '../', '/', '~/', '~\\', or a Windows drive
        letter (e.g. 'C:\\' or 'C:/').
        Protocol-relative URLs ('//...') are explicitly excluded.
        """
        s = dep_str.strip()
        # Reject protocol-relative URLs ('//...')
        if s.startswith("//"):
            return False
        if s.startswith(('./','../', '/', '~/', '~\\', '.\\', '..\\')):
            return True
        # Windows absolute paths: drive letter + colon + separator (C:\ or C:/).
        # Only ASCII letters A-Z/a-z are valid drive letters.
        if (
            len(s) >= 3
            and (('A' <= s[0] <= 'Z') or ('a' <= s[0] <= 'z'))
            and s[1] == ':'
            and s[2] in ('\\', '/')
        ):
            return True
        return False

    def get_unique_key(self) -> str:
        """Get a unique key for this dependency for deduplication.

        For regular packages: repo_url
        For virtual packages: repo_url + virtual_path to ensure uniqueness
        For local packages: the local_path

        Returns:
            str: Unique key for this dependency
        """
        if self.is_local and self.local_path:
            return self.local_path
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url

    def to_canonical(self) -> str:
        """Return the canonical scheme-free identity string for this dependency.

        Follows the Docker-style default-registry convention:
        - Default host (github.com) is stripped  ->  owner/repo
        - Non-default hosts are preserved         ->  gitlab.com/owner/repo
        - Virtual paths are appended              ->  owner/repo/path/to/thing
        - Refs are appended with #                ->  owner/repo#v1.0
        - Local paths are returned as-is          ->  ./packages/my-pkg

        No .git suffix, no git@, and no transport scheme -- just the canonical
        identifier. Use ``to_apm_yml_entry()`` when the serialized apm.yml value
        must preserve an explicit ``http://`` transport.

        Returns:
            str: Canonical dependency string
        """
        if self.is_local and self.local_path:
            return self.local_path

        host = self.host or default_host()

        is_default = host.lower() == default_host().lower()
        # Custom port is part of the transport and must travel with the host label.
        host_label = f"{host}:{self.port}" if self.port else host

        # Start with optional host prefix
        if is_default and not self.port and not self.artifactory_prefix:
            result = self.repo_url
        elif self.artifactory_prefix:
            result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
        else:
            result = f"{host_label}/{self.repo_url}"

        # Append virtual path for virtual packages
        if self.is_virtual and self.virtual_path:
            result = f"{result}/{self.virtual_path}"

        # Append reference (branch, tag, commit)
        if self.reference:
            result = f"{result}#{self.reference}"

        return result

    def get_identity(self) -> str:
        """Return the identity of this dependency (canonical form without ref/alias).

        Two deps with the same identity are the same package, regardless of
        which ref or alias they specify. Used for duplicate detection and uninstall matching.

        Returns:
            str: Identity string (e.g., "owner/repo" or "gitlab.com/owner/repo/path")
        """
        if self.is_local and self.local_path:
            return self.local_path

        host = self.host or default_host()
        is_default = host.lower() == default_host().lower()
        host_label = f"{host}:{self.port}" if self.port else host

        if is_default and not self.port and not self.artifactory_prefix:
            result = self.repo_url
        elif self.artifactory_prefix:
            result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
        else:
            result = f"{host_label}/{self.repo_url}"

        if self.is_virtual and self.virtual_path:
            result = f"{result}/{self.virtual_path}"

        return result

    @staticmethod
    def canonicalize(raw: str) -> str:
        """Parse any raw input form and return its canonical identifier form.

        Convenience method that combines parse() + to_canonical().

        Args:
            raw: Any supported input form (shorthand, FQDN, HTTPS, SSH, etc.)

        Returns:
            str: Canonical scheme-free identifier form
        """
        return DependencyReference.parse(raw).to_canonical()

    def get_canonical_dependency_string(self) -> str:
        """Get the host-blind canonical string for filesystem and orphan-detection matching.

        This returns repo_url (+ virtual_path) without host prefix -- it matches
        the filesystem layout in apm_modules/ which is also host-blind.

        For identity-based matching that includes non-default hosts, use get_identity().
        For the transport-aware apm.yml entry, use to_apm_yml_entry().

        Returns:
            str: Host-blind canonical string (e.g., "owner/repo")
        """
        return self.get_unique_key()

    def get_install_path(self, apm_modules_dir: Path) -> Path:
        """Get the canonical filesystem path where this package should be installed.

        This is the single source of truth for where a package lives in apm_modules/.

        For regular packages:
            - GitHub: apm_modules/owner/repo/
            - ADO: apm_modules/org/project/repo/

        For virtual file/collection packages:
            - GitHub: apm_modules/owner/<virtual-package-name>/
            - ADO: apm_modules/org/project/<virtual-package-name>/

        For subdirectory packages (Claude Skills, nested APM packages):
            - GitHub: apm_modules/owner/repo/subdir/path/
            - ADO: apm_modules/org/project/repo/subdir/path/

        For local packages:
            - apm_modules/_local/<directory-name>/

        Args:
            apm_modules_dir: Path to the apm_modules directory

        Raises:
            PathTraversalError: If the computed path escapes apm_modules_dir
        Returns:
            Path: Absolute path to the package installation directory
        """
        if self.is_local and self.local_path:
            pkg_dir_name = Path(self.local_path).name
            validate_path_segments(
                pkg_dir_name,
                context="local package path",
                reject_empty=True,
            )
            result = apm_modules_dir / "_local" / pkg_dir_name
            ensure_path_within(result, apm_modules_dir)
            return result

        repo_parts = self.repo_url.split("/")

        # Security: reject traversal in repo_url segments (catches lockfile injection)
        validate_path_segments(self.repo_url, context="repo_url")

        # Security: reject traversal in virtual_path (catches lockfile injection)
        if self.virtual_path:
            validate_path_segments(self.virtual_path, context="virtual_path")
        result: Path | None = None

        if self.is_virtual:
            # Subdirectory packages (like Claude Skills) should use natural path structure
            if self.is_virtual_subdirectory():
                # Use repo path + subdirectory path
                if self.is_azure_devops() and len(repo_parts) >= 3:
                    # ADO: org/project/repo/subdir
                    result = (
                        apm_modules_dir
                        / repo_parts[0]
                        / repo_parts[1]
                        / repo_parts[2]
                        / self.virtual_path
                    )
                elif len(repo_parts) >= 2:
                    # owner/repo/subdir or group/subgroup/repo/subdir
                    result = apm_modules_dir.joinpath(*repo_parts, self.virtual_path)
            else:
                # Virtual file/collection: use sanitized package name (flattened)
                package_name = self.get_virtual_package_name()
                if self.is_azure_devops() and len(repo_parts) >= 3:
                    # ADO: org/project/virtual-pkg-name
                    result = (
                        apm_modules_dir / repo_parts[0] / repo_parts[1] / package_name
                    )
                elif len(repo_parts) >= 2:
                    # owner/virtual-pkg-name (use first segment as namespace)
                    result = apm_modules_dir / repo_parts[0] / package_name
        else:
            # Regular package: use full repo path
            if self.is_azure_devops() and len(repo_parts) >= 3:
                # ADO: org/project/repo
                result = apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2]
            elif len(repo_parts) >= 2:
                # owner/repo or group/subgroup/repo (generic hosts)
                result = apm_modules_dir.joinpath(*repo_parts)

        if result is None:
            # Fallback: join all parts
            result = apm_modules_dir.joinpath(*repo_parts)

        # Security: ensure the computed path stays within apm_modules/
        ensure_path_within(result, apm_modules_dir)
        return result

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
        path = parsed.path.lstrip("/")
        fragment = parsed.fragment

        reference: Optional[str] = None
        alias: Optional[str] = None

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
        validate_path_segments(
            repo_url, context="SSH repository path", reject_empty=True
        )

        return host, port, repo_url, reference, alias

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
                    f"Object-style dependency must have a 'git' field, "
                    f"or 'path' must be a local filesystem path "
                    f"(starting with './', '../', '/', or '~')"
                )
            return cls.parse(local)

        if "git" not in entry:
            raise ValueError(
                "Object-style dependency must have a 'git' or 'path' field"
            )

        git_url = entry["git"]
        if not isinstance(git_url, str) or not git_url.strip():
            raise ValueError("'git' field must be a non-empty string")

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
                raise ValueError(
                    "'skills' field must be a list of skill names"
                )
            if len(skills_raw) == 0:
                raise ValueError(
                    "skills: must contain at least one name; "
                    "remove the field to install all skills in the bundle."
                )
            seen: set = set()
            validated: list = []
            for name in skills_raw:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "Each entry in 'skills' must be a non-empty string"
                    )
                name = name.strip()
                # Path safety: reject traversal sequences
                validate_path_segments(name, context="skills/<name>")
                if name not in seen:
                    seen.add(name)
                    validated.append(name)
            dep.skill_subset = sorted(validated)

        return dep

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
                        raise ValueError(
                            unsupported_host_error(hostname or first_segment)
                        )
                except (ValueError, AttributeError) as e:
                    if isinstance(e, ValueError) and "Invalid Git host" in str(e):
                        raise
                    raise ValueError(unsupported_host_error(first_segment))
            elif check_str.startswith("gh/"):
                check_str = "/".join(check_str.split("/")[1:])

        path_segments = [seg for seg in check_str.split("/") if seg]

        is_ado = validated_host is not None and is_azure_devops_hostname(validated_host)
        is_generic_host = (
            validated_host is not None
            and not is_github_hostname(validated_host)
            and not is_azure_devops_hostname(validated_host)
        )

        if is_ado and "_git" in path_segments:
            git_idx = path_segments.index("_git")
            path_segments = path_segments[:git_idx] + path_segments[git_idx + 1 :]

        # Detect Artifactory VCS paths (artifactory/{repo-key}/{owner}/{repo})
        is_artifactory = is_generic_host and is_artifactory_path(path_segments)

        if is_ado:
            min_base_segments = 3
        elif is_artifactory:
            # Artifactory: artifactory/{repo-key}/{owner}/{repo}
            min_base_segments = 4
        elif is_generic_host:
            has_virtual_ext = any(
                any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS)
                for seg in path_segments
            )
            has_collection = "collections" in path_segments
            if has_virtual_ext or has_collection:
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

            if "/collections/" in check_str or virtual_path.startswith("collections/"):
                pass
            elif any(virtual_path.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
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

    @staticmethod
    def _parse_ssh_url(dependency_str: str):
        """Parse an SCP-shorthand SSH URL (``git@host:owner/repo``).

        SCP shorthand cannot carry a port (``:`` is the path separator), so the
        returned port is always ``None``. For custom SSH ports, use the
        ``ssh://`` URL form which is handled by ``_parse_ssh_protocol_url``.

        Returns:
            ``(host, port, repo_url, reference, alias)`` or *None* if not an SCP URL.
        """
        ssh_match = re.match(r"^git@([^:]+):(.+)$", dependency_str)
        if not ssh_match:
            return None

        host = ssh_match.group(1)
        ssh_repo_part = ssh_match.group(2)

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
                    suggested = f"ssh://git@{host}:{port_candidate}/{remaining_path}{git_suffix}{ref_suffix}{alias_suffix}"
                    raise ValueError(
                        f"It looks like '{first_segment}' in 'git@{host}:{repo_url}' "
                        f"is a port number, but SCP-style URLs (git@host:path) cannot "
                        f"carry a port. Use the ssh:// URL form instead:\n"
                        f"  {suggested}"
                    )
                else:
                    raise ValueError(
                        f"It looks like '{first_segment}' in 'git@{host}:{first_segment}' "
                        f"is a port number, but no repository path follows it. "
                        f"SCP-style URLs (git@host:path) cannot carry a port. "
                        f"Use the ssh:// URL form: ssh://git@{host}:{port_candidate}/<owner>/<repo>.git"
                    )

        # Security: reject traversal sequences in SSH repo paths
        validate_path_segments(
            repo_url, context="SSH repository path", reject_empty=True
        )

        return host, None, repo_url, reference, alias

    @classmethod
    def _resolve_virtual_shorthand_repo(cls, repo_url, validated_host):
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
                if len(parts) < 5:
                    raise ValueError(
                        "Invalid Azure DevOps virtual package format: must be dev.azure.com/org/project/repo/path"
                    )
                repo_url = "/".join(parts[1:4])
            elif is_artifactory_path(parts[1:]):
                art_result = parse_artifactory_path(parts[1:])
                if art_result:
                    repo_url = f"{art_result[1]}/{art_result[2]}"
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
            if is_azure_devops_hostname(host) and len(parts) >= 4:
                user_repo = "/".join(parts[1:4])
            elif not is_github_hostname(host) and not is_azure_devops_hostname(
                host
            ):
                if is_artifactory_path(parts[1:]):
                    art_result = parse_artifactory_path(parts[1:])
                    if art_result:
                        user_repo = f"{art_result[1]}/{art_result[2]}"
                    else:
                        user_repo = "/".join(parts[1:])
                else:
                    user_repo = "/".join(parts[1:])
            else:
                user_repo = "/".join(parts[1:3])
        elif len(parts) >= 2 and "." not in parts[0]:
            if not host:
                host = default_host()
            if is_azure_devops_hostname(host) and len(parts) >= 3:
                user_repo = "/".join(parts[:3])
            elif (
                host
                and not is_github_hostname(host)
                and not is_azure_devops_hostname(host)
            ):
                user_repo = "/".join(parts)
            else:
                user_repo = "/".join(parts[:2])
        else:
            raise ValueError(
                f"Use 'user/repo' or 'github.com/user/repo' or 'dev.azure.com/org/project/repo' format"
            )

        if not user_repo or "/" not in user_repo:
            raise ValueError(
                f"Invalid repository format: {repo_url}. Expected 'user/repo' or 'org/project/repo'"
            )

        uparts = user_repo.split("/")
        is_ado_host = host and is_azure_devops_hostname(host)

        if is_ado_host:
            if len(uparts) < 3:
                raise ValueError(
                    f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
                )
        else:
            if len(uparts) < 2:
                raise ValueError(
                    f"Invalid repository format: {repo_url}. Expected 'user/repo'"
                )

        allowed_pattern = (
            r"^[a-zA-Z0-9._\- ]+$" if is_ado_host else r"^[a-zA-Z0-9._-]+$"
        )
        validate_path_segments(
            "/".join(uparts), context="repository path"
        )
        for part in uparts:
            if not re.match(allowed_pattern, part.rstrip(".git")):
                raise ValueError(f"Invalid repository path component: {part}")

        quoted_repo = "/".join(urllib.parse.quote(p, safe="") for p in uparts)
        github_url = urllib.parse.urljoin(f"https://{host}/", quoted_repo)
        parsed_url = urllib.parse.urlparse(github_url)

        return parsed_url, host

    @classmethod
    def _validate_url_repo_path(cls, parsed_url):
        """Validate and normalise the repository path from a parsed URL.

        Checks host support, strips ``.git`` suffixes, removes ``_git``
        segments, and validates each path component against the allowed
        character set for the detected host type.

        Returns:
            repo_url (str): Normalised repository path
                (e.g. ``owner/repo`` or ``org/project/repo``).
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
            if len(path_parts) != 3:
                raise ValueError(
                    f"Invalid Azure DevOps repository path: expected 'org/project/repo', got '{path}'"
                )
        else:
            if len(path_parts) < 2:
                raise ValueError(
                    f"Invalid repository path: expected at least 'user/repo', got '{path}'"
                )
            for pp in path_parts:
                if any(pp.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
                    raise ValueError(
                        f"Invalid repository path: '{path}' contains a virtual file extension. "
                        f"Use the dict format with 'path:' for virtual packages in HTTPS URLs"
                    )

        allowed_pattern = (
            r"^[a-zA-Z0-9._\- ]+$" if is_ado_host else r"^[a-zA-Z0-9._-]+$"
        )
        validate_path_segments(
            "/".join(path_parts),
            context="repository URL path",
            reject_empty=True,
        )
        for part in path_parts:
            if not re.match(allowed_pattern, part):
                raise ValueError(f"Invalid repository path component: {part}")

        return "/".join(path_parts)

    @classmethod
    def _parse_standard_url(
        cls, dependency_str: str, is_virtual_package: bool, virtual_path, validated_host
    ):
        """Parse a non-SSH dependency string (HTTPS, FQDN, or shorthand).

        Detects scheme vs shorthand, delegates host-specific resolution to
        helpers, then validates the resulting URL path.

        Returns:
            ``(host, port, repo_url, reference, alias)``
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
                repo_url, validated_host
            )

        # Normalize to URL format for secure parsing
        if repo_url_lower.startswith(("https://", "http://")):
            parsed_url = urllib.parse.urlparse(repo_url)
            host = parsed_url.hostname or ""
            port = parsed_url.port  # capture :PORT from https://host:8443/...
        else:
            parsed_url, host = cls._resolve_shorthand_to_parsed_url(repo_url, host)

        repo_url = cls._validate_url_repo_path(parsed_url)

        if not host:
            host = default_host()

        return host, port, repo_url, reference, alias

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
            if not re.match(
                r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._\- ]+/[a-zA-Z0-9._\- ]+$", repo_url
            ):
                raise ValueError(
                    f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
                )
            ado_parts = repo_url.split("/")
            validate_path_segments(
                repo_url, context="Azure DevOps repository path"
            )
            return ado_parts[0], ado_parts[1], ado_parts[2]

        segments = repo_url.split("/")
        if len(segments) < 2:
            raise ValueError(
                f"Invalid repository format: {repo_url}. Expected 'user/repo'"
            )
        if not all(re.match(r"^[a-zA-Z0-9._-]+$", s) for s in segments):
            raise ValueError(
                f"Invalid repository format: {repo_url}. Contains invalid characters"
            )
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
        - user/repo/collections/name (virtual collection package)
        - https://gitlab.com/owner/repo.git (generic HTTPS git URL)
        - git@gitlab.com:owner/repo.git (SSH git URL)
        - ssh://git@gitlab.com/owner/repo.git (SSH protocol URL)

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
                unsupported_host_error(
                    "//...", context="Protocol-relative URLs are not supported"
                )
            )

        # Phase 1: detect virtual packages
        is_virtual_package, virtual_path, validated_host = cls._detect_virtual_package(
            dependency_str
        )

        # Phase 2: parse SSH (ssh:// URL first -- it preserves port; then SCP
        # shorthand), otherwise fall back to HTTPS/shorthand parsing.
        explicit_scheme: Optional[str] = None
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
                host, port, repo_url, reference, alias = cls._parse_standard_url(
                    dependency_str, is_virtual_package, virtual_path, validated_host
                )
                _stripped = dependency_str.strip().lower()
                if _stripped.startswith("https://"):
                    explicit_scheme = "https"
                elif _stripped.startswith("http://"):
                    explicit_scheme = "http"

        # Phase 3: final validation and ADO field extraction
        ado_organization, ado_project, ado_repo = cls._validate_final_repo_fields(
            host, repo_url
        )

        if alias and not re.match(r"^[a-zA-Z0-9._-]+$", alias):
            raise ValueError(
                f"Invalid alias: {alias}. Aliases can only contain letters, numbers, dots, underscores, and hyphens"
            )

        # Extract Artifactory prefix from the original path if applicable
        is_ado_final = host and is_azure_devops_hostname(host)
        artifactory_prefix = None
        if host and not is_ado_final:
            artifactory_prefix = cls._extract_artifactory_prefix(
                dependency_str, host
            )

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

    def to_apm_yml_entry(self):
        """Return the entry to store in apm.yml.

        For HTTP (insecure) deps, returns a dict with 'git' and 'allow_insecure' keys.
        For deps with skill_subset, returns a dict with 'git' and 'skills' keys.
        For all other deps, returns the canonical string (same as to_canonical()).

        Returns:
            str or dict: String for simple deps; dict for HTTP or skill-subset deps.
        """
        if self.is_insecure:
            host = self.host or default_host()
            entry = {"git": f"http://{host}/{self.repo_url}"}
            if self.reference:
                entry["ref"] = self.reference
            if self.alias:
                entry["alias"] = self.alias
            entry["allow_insecure"] = self.allow_insecure
            if self.skill_subset:
                entry["skills"] = sorted(self.skill_subset)
            return entry
        if self.skill_subset:
            entry = {"git": self.get_identity()}
            if self.reference:
                entry["ref"] = self.reference
            if self.alias:
                entry["alias"] = self.alias
            entry["skills"] = sorted(self.skill_subset)
            return entry
        return self.to_canonical()

    def to_github_url(self) -> str:
        """Convert to full repository URL.

        For Azure DevOps, generates: https://dev.azure.com/org/project/_git/repo
        For GitHub, generates: https://github.com/owner/repo
        For local packages, returns the local path.
        """
        if self.is_local and self.local_path:
            return self.local_path

        host = self.host or default_host()
        netloc = f"{host}:{self.port}" if self.port else host

        scheme = "http" if self.is_insecure else "https"

        if self.is_azure_devops():
            # ADO format: https://dev.azure.com/org/project/_git/repo
            project = urllib.parse.quote(self.ado_project, safe="")
            repo = urllib.parse.quote(self.ado_repo, safe="")
            return f"https://{netloc}/{self.ado_organization}/{project}/_git/{repo}"
        elif self.artifactory_prefix:
            return f"{scheme}://{netloc}/{self.artifactory_prefix}/{self.repo_url}"
        else:
            # Git host format: https://github.com/owner/repo
            return f"{scheme}://{netloc}/{self.repo_url}"

    def to_clone_url(self) -> str:
        """Convert to a clone-friendly URL (same as to_github_url for most purposes)."""
        return self.to_github_url()

    def get_display_name(self) -> str:
        """Get display name for this dependency (alias or repo name)."""
        if self.alias:
            return self.alias
        if self.is_local and self.local_path:
            return self.local_path
        if self.is_virtual:
            return self.get_virtual_package_name()
        return self.repo_url  # Full repo URL for disambiguation

    def __str__(self) -> str:
        """String representation of the dependency reference."""
        if self.is_local and self.local_path:
            return self.local_path
        if self.host:
            host_label = f"{self.host}:{self.port}" if self.port else self.host
            if self.artifactory_prefix:
                result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
            else:
                result = f"{host_label}/{self.repo_url}"
        else:
            result = self.repo_url
        if self.virtual_path:
            result += f"/{self.virtual_path}"
        if self.reference:
            result += f"#{self.reference}"
        if self.alias:
            result += f"@{self.alias}"
        return result
