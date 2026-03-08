"""APM Package data models and validation logic."""

import re
import urllib.parse
from ..utils.github_host import is_supported_git_host, is_azure_devops_hostname, is_github_hostname, default_host, unsupported_host_error
import yaml
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

# Module-level parse cache: resolved path -> APMPackage (#171)
_apm_yml_cache: Dict[Path, "APMPackage"] = {}


def clear_apm_yml_cache() -> None:
    """Clear the from_apm_yml parse cache. Call in tests for isolation."""
    _apm_yml_cache.clear()


class GitReferenceType(Enum):
    """Types of Git references supported."""
    BRANCH = "branch"
    TAG = "tag" 
    COMMIT = "commit"


class PackageType(Enum):
    """Types of packages that APM can install.
    
    This enum is used internally to classify packages based on their content
    (presence of apm.yml, SKILL.md, hooks/, plugin.json, etc.).
    """
    APM_PACKAGE = "apm_package"      # Has apm.yml
    CLAUDE_SKILL = "claude_skill"    # Has SKILL.md, no apm.yml
    HOOK_PACKAGE = "hook_package"    # Has hooks/hooks.json, no apm.yml or SKILL.md
    HYBRID = "hybrid"                # Has both apm.yml and SKILL.md
    MARKETPLACE_PLUGIN = "marketplace_plugin"  # Has plugin.json, no apm.yml
    INVALID = "invalid"              # None of the above


class PackageContentType(Enum):
    """Explicit package content type declared in apm.yml.
    
    This is the user-facing `type` field in apm.yml that controls how the
    package is processed during install/compile:
    - INSTRUCTIONS: Compile to AGENTS.md only, no skill created
    - SKILL: Install as native skill only, no AGENTS.md compilation
    - HYBRID: Both AGENTS.md instructions AND skill installation (default)
    - PROMPTS: Commands/prompts only, no instructions or skills
    """
    INSTRUCTIONS = "instructions"  # Compile to AGENTS.md only
    SKILL = "skill"               # Install as native skill only
    HYBRID = "hybrid"             # Both (default)
    PROMPTS = "prompts"           # Commands/prompts only
    
    @classmethod
    def from_string(cls, value: str) -> "PackageContentType":
        """Parse a string value into a PackageContentType enum.
        
        Args:
            value: String value to parse (e.g., "instructions", "skill")
            
        Returns:
            PackageContentType: The corresponding enum value
            
        Raises:
            ValueError: If the value is not a valid package content type
        """
        if not value:
            raise ValueError("Package type cannot be empty")
        
        value_lower = value.lower().strip()
        for member in cls:
            if member.value == value_lower:
                return member
        
        valid_types = ", ".join(f"'{m.value}'" for m in cls)
        raise ValueError(
            f"Invalid package type '{value}'. "
            f"Valid types are: {valid_types}"
        )


class ValidationError(Enum):
    """Types of validation errors for APM packages."""
    MISSING_APM_YML = "missing_apm_yml"
    MISSING_APM_DIR = "missing_apm_dir"
    INVALID_YML_FORMAT = "invalid_yml_format"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_VERSION_FORMAT = "invalid_version_format"
    INVALID_DEPENDENCY_FORMAT = "invalid_dependency_format"
    EMPTY_APM_DIR = "empty_apm_dir"
    INVALID_PRIMITIVE_STRUCTURE = "invalid_primitive_structure"


class InvalidVirtualPackageExtensionError(ValueError):
    """Raised when a virtual package file has an invalid extension."""
    pass


@dataclass
class ResolvedReference:
    """Represents a resolved Git reference."""
    original_ref: str
    ref_type: GitReferenceType
    resolved_commit: str
    ref_name: str  # The actual branch/tag/commit name
    
    def __str__(self) -> str:
        """String representation of resolved reference."""
        if self.ref_type == GitReferenceType.COMMIT:
            return f"{self.resolved_commit[:8]}"
        return f"{self.ref_name} ({self.resolved_commit[:8]})"


@dataclass 
class DependencyReference:
    """Represents a reference to an APM dependency."""
    repo_url: str  # e.g., "user/repo" for GitHub or "org/project/repo" for Azure DevOps
    host: Optional[str] = None  # Optional host (github.com, dev.azure.com, or enterprise host)
    reference: Optional[str] = None  # e.g., "main", "v1.0.0", "abc123"
    alias: Optional[str] = None  # Optional alias for the dependency
    virtual_path: Optional[str] = None  # Path for virtual packages (e.g., "prompts/file.prompt.md")
    is_virtual: bool = False  # True if this is a virtual package (individual file or collection)
    
    # Azure DevOps specific fields (ADO uses org/project/repo structure)
    ado_organization: Optional[str] = None  # e.g., "dmeppiel-org"
    ado_project: Optional[str] = None       # e.g., "market-js-app"
    ado_repo: Optional[str] = None          # e.g., "compliance-rules"
    
    # Supported file extensions for virtual packages
    VIRTUAL_FILE_EXTENSIONS = ('.prompt.md', '.instructions.md', '.chatmode.md', '.agent.md')
    
    def is_azure_devops(self) -> bool:
        """Check if this reference points to Azure DevOps."""
        from ..utils.github_host import is_azure_devops_hostname
        return self.host is not None and is_azure_devops_hostname(self.host)
    
    def is_virtual_file(self) -> bool:
        """Check if this is a virtual file package (individual file)."""
        if not self.is_virtual or not self.virtual_path:
            return False
        return any(self.virtual_path.endswith(ext) for ext in self.VIRTUAL_FILE_EXTENSIONS)
    
    def is_virtual_collection(self) -> bool:
        """Check if this is a virtual collection package."""
        if not self.is_virtual or not self.virtual_path:
            return False
        # Collections have /collections/ in their path or start with collections/
        return '/collections/' in self.virtual_path or self.virtual_path.startswith('collections/')
    
    def is_virtual_subdirectory(self) -> bool:
        """Check if this is a virtual subdirectory package (e.g., Claude Skill).
        
        A subdirectory package is a virtual package that:
        - Has a virtual_path that is NOT a file extension we recognize
        - Is NOT a collection (doesn't have /collections/ in path)
        - Is a directory path (likely containing SKILL.md or apm.yml)
        
        Examples:
            - ComposioHQ/awesome-claude-skills/brand-guidelines → True
            - owner/repo/prompts/file.prompt.md → False (is_virtual_file)
            - owner/repo/collections/name → False (is_virtual_collection)
        """
        if not self.is_virtual or not self.virtual_path:
            return False
        # Not a file and not a collection = subdirectory
        return not self.is_virtual_file() and not self.is_virtual_collection()
    
    def get_virtual_package_name(self) -> str:
        """Generate a package name for this virtual package.
        
        For virtual packages, we create a sanitized name from the path:
        - owner/repo/prompts/code-review.prompt.md → repo-code-review
        - owner/repo/collections/project-planning → repo-project-planning
        - owner/repo/collections/project-planning.collection.yml → repo-project-planning
        """
        if not self.is_virtual or not self.virtual_path:
            return self.repo_url.split('/')[-1]  # Return repo name as fallback
        
        # Extract repo name and file/collection name
        repo_parts = self.repo_url.split('/')
        repo_name = repo_parts[-1] if repo_parts else "package"
        
        # Get the basename without extension
        path_parts = self.virtual_path.split('/')
        if self.is_virtual_collection():
            # For collections: use the collection name without extension
            # collections/project-planning → project-planning
            # collections/project-planning.collection.yml → project-planning
            collection_name = path_parts[-1]
            # Strip .collection.yml/.collection.yaml extension if present
            for ext in ('.collection.yml', '.collection.yaml'):
                if collection_name.endswith(ext):
                    collection_name = collection_name[:-len(ext)]
                    break
            return f"{repo_name}-{collection_name}"
        else:
            # For individual files: use the filename without extension
            # prompts/code-review.prompt.md → code-review
            filename = path_parts[-1]
            for ext in self.VIRTUAL_FILE_EXTENSIONS:
                if filename.endswith(ext):
                    filename = filename[:-len(ext)]
                    break
            return f"{repo_name}-{filename}"
    
    def get_unique_key(self) -> str:
        """Get a unique key for this dependency for deduplication.
        
        For regular packages: repo_url
        For virtual packages: repo_url + virtual_path to ensure uniqueness
        
        Returns:
            str: Unique key for this dependency
        """
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url
    
    def to_canonical(self) -> str:
        """Return the canonical form of this dependency for storage in apm.yml.
        
        Follows the Docker-style default-registry convention:
        - Default host (github.com) is stripped  →  owner/repo
        - Non-default hosts are preserved         →  gitlab.com/owner/repo
        - Virtual paths are appended              →  owner/repo/path/to/thing
        - Refs are appended with #                →  owner/repo#v1.0
        - Aliases are appended with @             →  owner/repo@my-alias
        
        No .git suffix, no https://, no git@ — just the canonical identifier.
        
        Returns:
            str: Canonical dependency string
        """
        host = self.host or default_host()
        is_default = host.lower() == default_host().lower()
        
        # Start with optional host prefix
        if is_default:
            result = self.repo_url
        else:
            result = f"{host}/{self.repo_url}"
        
        # Append virtual path for virtual packages
        if self.is_virtual and self.virtual_path:
            result = f"{result}/{self.virtual_path}"
        
        # Append reference (branch, tag, commit)
        if self.reference:
            result = f"{result}#{self.reference}"
        
        # Append alias
        if self.alias:
            result = f"{result}@{self.alias}"
        
        return result
    
    def get_identity(self) -> str:
        """Return the identity of this dependency (canonical form without ref/alias).
        
        Two deps with the same identity are the same package, regardless of
        which ref or alias they specify. Used for duplicate detection and uninstall matching.
        
        Returns:
            str: Identity string (e.g., "owner/repo" or "gitlab.com/owner/repo/path")
        """
        host = self.host or default_host()
        is_default = host.lower() == default_host().lower()
        
        if is_default:
            result = self.repo_url
        else:
            result = f"{host}/{self.repo_url}"
        
        if self.is_virtual and self.virtual_path:
            result = f"{result}/{self.virtual_path}"
        
        return result
    
    @staticmethod
    def canonicalize(raw: str) -> str:
        """Parse any raw input form and return its canonical storage form.
        
        Convenience method that combines parse() + to_canonical().
        
        Args:
            raw: Any supported input form (shorthand, FQDN, HTTPS, SSH, etc.)
            
        Returns:
            str: Canonical form for apm.yml storage
        """
        return DependencyReference.parse(raw).to_canonical()
    
    def get_canonical_dependency_string(self) -> str:
        """Get the host-blind canonical string for filesystem and orphan-detection matching.
        
        This returns repo_url (+ virtual_path) without host prefix — it matches
        the filesystem layout in apm_modules/ which is also host-blind.
        
        For identity-based matching that includes non-default hosts, use get_identity().
        For the full canonical form suitable for apm.yml storage, use to_canonical().
        
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
        
        Args:
            apm_modules_dir: Path to the apm_modules directory
            
        Returns:
            Path: Absolute path to the package installation directory
        """
        repo_parts = self.repo_url.split("/")
        
        if self.is_virtual:
            # Subdirectory packages (like Claude Skills) should use natural path structure
            if self.is_virtual_subdirectory():
                # Use repo path + subdirectory path
                if self.is_azure_devops() and len(repo_parts) >= 3:
                    # ADO: org/project/repo/subdir
                    return apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2] / self.virtual_path
                elif len(repo_parts) >= 2:
                    # owner/repo/subdir or group/subgroup/repo/subdir
                    return apm_modules_dir.joinpath(*repo_parts, self.virtual_path)
            else:
                # Virtual file/collection: use sanitized package name (flattened)
                package_name = self.get_virtual_package_name()
                if self.is_azure_devops() and len(repo_parts) >= 3:
                    # ADO: org/project/virtual-pkg-name
                    return apm_modules_dir / repo_parts[0] / repo_parts[1] / package_name
                elif len(repo_parts) >= 2:
                    # owner/virtual-pkg-name (use first segment as namespace)
                    return apm_modules_dir / repo_parts[0] / package_name
        else:
            # Regular package: use full repo path
            if self.is_azure_devops() and len(repo_parts) >= 3:
                # ADO: org/project/repo
                return apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2]
            elif len(repo_parts) >= 2:
                # owner/repo or group/subgroup/repo (generic hosts)
                return apm_modules_dir.joinpath(*repo_parts)
        
        # Fallback: join all parts
        return apm_modules_dir.joinpath(*repo_parts)
    
    @staticmethod
    def _normalize_ssh_protocol_url(url: str) -> str:
        """Normalize ssh:// protocol URLs to git@ format for consistent parsing.
        
        Converts:
        - ssh://git@gitlab.com/owner/repo.git → git@gitlab.com:owner/repo.git
        - ssh://git@host:port/owner/repo.git → git@host:owner/repo.git
        
        Non-SSH URLs are returned unchanged.
        """
        if not url.startswith('ssh://'):
            return url
        
        # Parse the ssh:// URL
        # Format: ssh://[user@]host[:port]/path
        remainder = url[6:]  # Remove 'ssh://'
        
        # Extract user if present (typically 'git@')
        user_prefix = ""
        if '@' in remainder.split('/')[0]:
            user_at_idx = remainder.index('@')
            user_prefix = remainder[:user_at_idx + 1]  # e.g., "git@"
            remainder = remainder[user_at_idx + 1:]
        
        # Extract host (and optional port)
        slash_idx = remainder.find('/')
        if slash_idx == -1:
            return url  # Invalid format, return as-is
        
        host_part = remainder[:slash_idx]
        path_part = remainder[slash_idx + 1:]
        
        # Strip port if present (e.g., host:22)
        if ':' in host_part:
            host_part = host_part.split(':')[0]
        
        # Convert to git@ format: git@host:path
        if user_prefix:
            return f"{user_prefix}{host_part}:{path_part}"
        else:
            return f"git@{host_part}:{path_part}"

    @classmethod
    def parse_from_dict(cls, entry: dict) -> "DependencyReference":
        """Parse an object-style dependency entry from apm.yml.
        
        Supports the Cargo-inspired object format:
        
            - git: https://gitlab.com/acme/coding-standards.git
              path: instructions/security
              ref: v2.0
        
            - git: git@bitbucket.org:team/rules.git
              path: prompts/review.prompt.md
        
        Args:
            entry: Dictionary with 'git' (required), 'path' (optional), 'ref' (optional)
            
        Returns:
            DependencyReference: Parsed dependency reference
            
        Raises:
            ValueError: If the entry is missing required fields or has invalid format
        """
        if 'git' not in entry:
            raise ValueError("Object-style dependency must have a 'git' field")
        
        git_url = entry['git']
        if not isinstance(git_url, str) or not git_url.strip():
            raise ValueError("'git' field must be a non-empty string")
        
        sub_path = entry.get('path')
        ref_override = entry.get('ref')
        alias_override = entry.get('alias')
        
        # Validate sub_path if provided
        if sub_path is not None:
            if not isinstance(sub_path, str) or not sub_path.strip():
                raise ValueError("'path' field must be a non-empty string")
            sub_path = sub_path.strip().strip('/')
        
        # Parse the git URL using the standard parser
        dep = cls.parse(git_url)
        
        # Apply overrides from the object fields
        if ref_override is not None:
            if not isinstance(ref_override, str) or not ref_override.strip():
                raise ValueError("'ref' field must be a non-empty string")
            dep.reference = ref_override.strip()
        
        if alias_override is not None:
            if not isinstance(alias_override, str) or not alias_override.strip():
                raise ValueError("'alias' field must be a non-empty string")
            dep.alias = alias_override.strip()
        
        # Apply sub-path as virtual package
        if sub_path:
            dep.virtual_path = sub_path
            dep.is_virtual = True
        
        return dep

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

        # Decode percent-encoded characters (e.g., %20 for spaces in ADO project names)
        dependency_str = urllib.parse.unquote(dependency_str)

        # Check for control characters (newlines, tabs, etc.)
        if any(ord(c) < 32 for c in dependency_str):
            raise ValueError("Dependency string contains invalid control characters")
        
        # SECURITY: Reject protocol-relative URLs (//example.com)
        if dependency_str.startswith('//'):
            raise ValueError(unsupported_host_error("//...", context="Protocol-relative URLs are not supported"))
        
        # Normalize ssh:// protocol URLs to git@ format
        dependency_str = cls._normalize_ssh_protocol_url(dependency_str)
        
        # Early detection of virtual packages (3+ path segments)
        # Extract the core path before processing reference (#) and alias (@)
        work_str = dependency_str
        
        # Temporarily remove reference and alias for path segment counting
        temp_str = work_str
        if '@' in temp_str and not temp_str.startswith('git@'):
            temp_str = temp_str.rsplit('@', 1)[0]
        if '#' in temp_str:
            temp_str = temp_str.rsplit('#', 1)[0]
        
        # Check if this looks like a virtual package (3+ path segments)
        # Skip SSH URLs (git@host:owner/repo format)
        is_virtual_package = False
        virtual_path = None
        validated_host = None  # Track if we validated a GitHub hostname
        
        if not temp_str.startswith(('git@', 'https://', 'http://')):
            # SECURITY: Use proper URL parsing instead of substring checks to validate hostnames
            # This prevents bypasses like "evil.com/github.com/repo" or "github.com.evil.com/repo"
            check_str = temp_str
            
            # Try to parse as potential URL with host prefix
            if '/' in check_str:
                first_segment = check_str.split('/')[0]
                
                # If first segment contains a dot, it might be a hostname - VALIDATE IT
                if '.' in first_segment:
                    # Construct a full URL and parse it properly
                    test_url = f"https://{check_str}"
                    try:
                        parsed = urllib.parse.urlparse(test_url)
                        hostname = parsed.hostname
                        
                        # SECURITY CRITICAL: If there's a dot in first segment, it MUST be a valid Git hostname
                        # Otherwise reject it - prevents evil-github.com, github.com.evil.com attacks
                        if hostname and is_supported_git_host(hostname):
                            # Valid Git hosting hostname - extract path after it
                            validated_host = hostname
                            path_parts = parsed.path.lstrip('/').split('/')
                            if len(path_parts) >= 2:
                                # Remove the hostname from check_str by taking everything after first segment
                                check_str = '/'.join(check_str.split('/')[1:])
                        else:
                            # First segment has a dot but is NOT a valid Git host - REJECT
                            raise ValueError(
                                unsupported_host_error(hostname or first_segment)
                            )
                    except (ValueError, AttributeError) as e:
                        # If we can't parse or validate, and first segment has dot, it's suspicious - REJECT
                        if isinstance(e, ValueError) and "Invalid Git host" in str(e):
                            raise  # Re-raise our security error
                        raise ValueError(
                            unsupported_host_error(first_segment)
                        )
                elif check_str.startswith('gh/'):
                    # Handle 'gh/' shorthand - only if it's exactly at the start
                    check_str = '/'.join(check_str.split('/')[1:])
            
            # Count segments (owner/repo/path/to/file = 5 segments)
            path_segments = check_str.split('/')
            
            # Filter out empty segments (from double slashes like "user//repo")
            path_segments = [seg for seg in path_segments if seg]
            
            # For Azure DevOps, the base package format is org/project/repo (3 segments)
            # Virtual packages would have 4+ segments: org/project/repo/path/to/file
            # For GitHub, base is owner/repo (2 segments), virtual is 3+ segments
            # For generic hosts (GitLab, Gitea, etc.), all segments are repo path
            # unless virtual indicators (file extensions, collections) are present
            is_ado = validated_host is not None and is_azure_devops_hostname(validated_host)
            is_generic_host = (validated_host is not None
                               and not is_github_hostname(validated_host)
                               and not is_azure_devops_hostname(validated_host))
            
            # Handle _git in ADO URLs: org/project/_git/repo -> org/project/repo
            if is_ado and '_git' in path_segments:
                git_idx = path_segments.index('_git')
                # Remove _git from the path segments
                path_segments = path_segments[:git_idx] + path_segments[git_idx+1:]
            
            if is_ado:
                min_base_segments = 3
            elif is_generic_host:
                # For generic hosts (GitLab, Gitea), check for virtual indicators
                # If present, use 2-segment base (simple owner/repo + virtual path)
                # If absent, treat ALL segments as the repo path (nested groups)
                has_virtual_ext = any(
                    any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS)
                    for seg in path_segments
                )
                has_collection = 'collections' in path_segments
                if has_virtual_ext or has_collection:
                    min_base_segments = 2  # Simple repo with virtual path
                else:
                    min_base_segments = len(path_segments)  # All segments = repo path
            else:
                min_base_segments = 2  # GitHub: owner/repo
            min_virtual_segments = min_base_segments + 1
            
            if len(path_segments) >= min_virtual_segments:
                # This is a virtual package!
                # For GitHub: owner/repo/path/to/file.prompt.md
                # For ADO: org/project/repo/path/to/file.prompt.md
                is_virtual_package = True
                
                # Extract virtual path (base repo is derived later)
                virtual_path = '/'.join(path_segments[min_base_segments:])
                
                # Virtual package types (validated later during download):
                # 1. Collections: /collections/ in path
                # 2. Individual files: ends with .prompt.md, .agent.md, etc.
                # 3. Subdirectory packages: directory path (may contain apm.yml or SKILL.md)
                #    This allows Claude Skills and nested APM packages in monorepos
                if '/collections/' in check_str or virtual_path.startswith('collections/'):
                    # Collection virtual package - validated by fetching .collection.yml
                    pass
                elif any(virtual_path.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
                    # Individual file virtual package - valid extension
                    pass
                else:
                    # Check if it looks like a file (has extension) vs directory
                    last_segment = virtual_path.split('/')[-1]
                    if '.' in last_segment:
                        # Looks like a file with unknown extension - reject
                        raise InvalidVirtualPackageExtensionError(
                            f"Invalid virtual package path '{virtual_path}'. "
                            f"Individual files must end with one of: {', '.join(cls.VIRTUAL_FILE_EXTENSIONS)}. "
                            f"For subdirectory packages, the path should not have a file extension."
                        )
                    # Subdirectory package - will be validated by checking for apm.yml or SKILL.md
        
        # Handle SSH URLs first (before @ processing) to avoid conflict with alias separator
        original_str = dependency_str
        ssh_repo_part = None
        host = None
        # Match patterns like git@host:owner/repo.git
        ssh_match = re.match(r'^git@([^:]+):(.+)$', dependency_str)
        if ssh_match:
            host = ssh_match.group(1)
            ssh_repo_part = ssh_match.group(2)

            # Handle reference and alias in SSH URL (extract before .git stripping)
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

            # Strip .git suffix after extracting ref and alias
            if repo_part.endswith('.git'):
                repo_part = repo_part[:-4]

            repo_url = repo_part.strip()
        else:
            # Handle alias (@alias) for non-SSH URLs
            alias = None
            if "@" in dependency_str:
                dependency_str, alias = dependency_str.rsplit("@", 1)
                alias = alias.strip()
            
            # Handle reference (#ref)
            reference = None
            if "#" in dependency_str:
                repo_part, reference = dependency_str.rsplit("#", 1)
                reference = reference.strip()
            else:
                repo_part = dependency_str
            
            # SECURITY: Use urllib.parse for all URL validation to avoid substring vulnerabilities
            
            repo_url = repo_part.strip()
            
            # For virtual packages, extract just the owner/repo part (or org/project/repo for ADO)
            if is_virtual_package and not repo_url.startswith(("https://", "http://")):
                # Virtual packages have format: owner/repo/path/to/file or host/owner/repo/path/to/file
                # For ADO: dev.azure.com/org/project/repo/path/to/file (4+ with host) or org/project/repo/path (3+ without host)
                parts = repo_url.split("/")
                
                # Handle _git in path: org/project/_git/repo -> org/project/repo
                if '_git' in parts:
                    git_idx = parts.index('_git')
                    parts = parts[:git_idx] + parts[git_idx+1:]
                
                # Check if starts with host
                if len(parts) >= 3 and is_supported_git_host(parts[0]):
                    host = parts[0]
                    # For ADO: dev.azure.com/org/project/repo/path -> extract org/project/repo
                    # For GitHub: github.com/owner/repo/path -> extract owner/repo
                    if is_azure_devops_hostname(parts[0]):
                        if len(parts) < 5:  # host + org + project + repo + at least one path segment
                            raise ValueError("Invalid Azure DevOps virtual package format: must be dev.azure.com/org/project/repo/path")
                        repo_url = "/".join(parts[1:4])  # org/project/repo
                    else:
                        # For virtual packages with host prefix, base is always 2 segments
                        # (virtual indicators already detected in early detection)
                        repo_url = "/".join(parts[1:3])  # owner/repo
                elif len(parts) >= 2:
                    # No host prefix
                    if not host:
                        host = default_host()
                    # Use validated_host to check if this is ADO
                    if validated_host and is_azure_devops_hostname(validated_host):
                        if len(parts) < 4:  # org + project + repo + at least one path segment
                            raise ValueError("Invalid Azure DevOps virtual package format: expected at least org/project/repo/path")
                        repo_url = "/".join(parts[:3])  # org/project/repo
                    else:
                        repo_url = "/".join(parts[:2])  # owner/repo
            
            # Normalize to URL format for secure parsing - always use urllib.parse, never substring checks
            if repo_url.startswith(("https://", "http://")):
                # Already a full URL - parse directly
                parsed_url = urllib.parse.urlparse(repo_url)
                host = parsed_url.hostname or ""
            else:
                # Safely construct a URL from various input formats. Support GitHub, GitHub Enterprise,
                # Azure DevOps, and other Git hosting platforms.
                parts = repo_url.split("/")
                
                # Handle _git in path for ADO URLs
                if '_git' in parts:
                    git_idx = parts.index('_git')
                    parts = parts[:git_idx] + parts[git_idx+1:]
                
                # host/user/repo  OR user/repo (no host)
                if len(parts) >= 3 and is_supported_git_host(parts[0]):
                    # Format with host prefix: github.com/user/repo OR dev.azure.com/org/project/repo
                    host = parts[0]
                    if is_azure_devops_hostname(host) and len(parts) >= 4:
                        # ADO format: dev.azure.com/org/project/repo
                        user_repo = "/".join(parts[1:4])
                    elif not is_github_hostname(host) and not is_azure_devops_hostname(host):
                        # Generic host (GitLab, Gitea, etc.): all segments after host = repo path
                        user_repo = "/".join(parts[1:])
                    else:
                        # GitHub format: github.com/user/repo
                        user_repo = "/".join(parts[1:3])
                elif len(parts) >= 2 and "." not in parts[0]:
                    # Format without host: user/repo or org/project/repo (for ADO)
                    if not host:
                        host = default_host()
                    # Check if default host is ADO
                    if is_azure_devops_hostname(host) and len(parts) >= 3:
                        user_repo = "/".join(parts[:3])  # org/project/repo
                    elif host and not is_github_hostname(host) and not is_azure_devops_hostname(host):
                        # Generic host: all segments = repo path
                        user_repo = "/".join(parts)
                    else:
                        user_repo = "/".join(parts[:2])  # user/repo
                else:
                    raise ValueError(f"Use 'user/repo' or 'github.com/user/repo' or 'dev.azure.com/org/project/repo' format")

                # Validate format before URL construction (security critical)
                if not user_repo or "/" not in user_repo:
                    raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo' or 'org/project/repo'")

                uparts = user_repo.split("/")
                is_ado_host = host and is_azure_devops_hostname(host)
                
                if is_ado_host:
                    if len(uparts) < 3:
                        raise ValueError(f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'")
                else:
                    if len(uparts) < 2:
                        raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")
                
                # Security: validate characters to prevent injection
                # ADO project names may contain spaces
                allowed_pattern = r'^[a-zA-Z0-9._\- ]+$' if is_ado_host else r'^[a-zA-Z0-9._-]+$'
                for part in uparts:
                    if not re.match(allowed_pattern, part.rstrip('.git')):
                        raise ValueError(f"Invalid repository path component: {part}")

                # Safely construct URL using detected host
                # Quote path components to handle spaces in ADO project names
                quoted_repo = '/'.join(urllib.parse.quote(p, safe='') for p in uparts)
                github_url = urllib.parse.urljoin(f"https://{host}/", quoted_repo)
                parsed_url = urllib.parse.urlparse(github_url)

            # SECURITY: Validate that this is actually a supported Git host URL.
            # Accept github.com, GitHub Enterprise, Azure DevOps, etc. Use parsed_url.hostname
            hostname = parsed_url.hostname or ""
            if not is_supported_git_host(hostname):
                raise ValueError(unsupported_host_error(hostname or parsed_url.netloc))
            
            # Extract and validate the path
            path = parsed_url.path.strip("/")
            if not path:
                raise ValueError("Repository path cannot be empty")
            
            # Remove .git suffix if present
            if path.endswith(".git"):
                path = path[:-4]
            
            # Handle _git in parsed path for ADO URLs
            # Decode percent-encoded path components (e.g., spaces in ADO project names)
            path_parts = [urllib.parse.unquote(p) for p in path.split("/")]
            if '_git' in path_parts:
                git_idx = path_parts.index('_git')
                path_parts = path_parts[:git_idx] + path_parts[git_idx+1:]

            # Validate path format based on host type
            is_ado_host = is_azure_devops_hostname(hostname)

            if is_ado_host:
                if len(path_parts) != 3:
                    raise ValueError(f"Invalid Azure DevOps repository path: expected 'org/project/repo', got '{path}'")
            else:
                if len(path_parts) < 2:
                    raise ValueError(f"Invalid repository path: expected at least 'user/repo', got '{path}'")
                # HTTPS URLs cannot embed virtual paths — reject virtual file extensions
                for pp in path_parts:
                    if any(pp.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
                        raise ValueError(
                            f"Invalid repository path: '{path}' contains a virtual file extension. "
                            f"Use the dict format with 'path:' for virtual packages in HTTPS URLs"
                        )

            # Validate all path parts contain only allowed characters
            # ADO project names may contain spaces
            allowed_pattern = r'^[a-zA-Z0-9._\- ]+$' if is_ado_host else r'^[a-zA-Z0-9._-]+$'
            for i, part in enumerate(path_parts):
                if not part:
                    raise ValueError(f"Invalid repository format: path component {i+1} cannot be empty")
                if not re.match(allowed_pattern, part):
                    raise ValueError(f"Invalid repository path component: {part}")

            repo_url = "/".join(path_parts)
            
            # If host not set via SSH or parsed parts, default to default_host()
            if not host:
                host = default_host()

        
        # Validate repo format based on host type
        is_ado_final = host and is_azure_devops_hostname(host)
        if is_ado_final:
            # ADO format: org/project/repo (3 segments, project may contain spaces)
            if not re.match(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._\- ]+/[a-zA-Z0-9._-]+$', repo_url):
                raise ValueError(f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'")
            # Extract ADO-specific fields
            ado_parts = repo_url.split('/')
            ado_organization = ado_parts[0]
            ado_project = ado_parts[1]
            ado_repo = ado_parts[2]
        else:
            # Non-ADO format: user/repo or group/subgroup/repo (2+ segments)
            segments = repo_url.split('/')
            if len(segments) < 2:
                raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")
            if not all(re.match(r'^[a-zA-Z0-9._-]+$', s) for s in segments):
                raise ValueError(f"Invalid repository format: {repo_url}. Contains invalid characters")
            # SSH/HTTPS URLs cannot embed virtual paths — reject virtual file extensions
            for seg in segments:
                if any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
                    raise ValueError(
                        f"Invalid repository format: '{repo_url}' contains a virtual file extension. "
                        f"Use the dict format with 'path:' for virtual packages in SSH/HTTPS URLs"
                    )
            ado_organization = None
            ado_project = None
            ado_repo = None
        
        # Validate alias characters if present
        if alias and not re.match(r'^[a-zA-Z0-9._-]+$', alias):
            raise ValueError(f"Invalid alias: {alias}. Aliases can only contain letters, numbers, dots, underscores, and hyphens")

        return cls(
            repo_url=repo_url,
            host=host,
            reference=reference,
            alias=alias,
            virtual_path=virtual_path,
            is_virtual=is_virtual_package,
            ado_organization=ado_organization,
            ado_project=ado_project,
            ado_repo=ado_repo
        )

    def to_github_url(self) -> str:
        """Convert to full repository URL.
        
        For Azure DevOps, generates: https://dev.azure.com/org/project/_git/repo
        For GitHub, generates: https://github.com/owner/repo
        """
        host = self.host or default_host()
        
        if self.is_azure_devops():
            # ADO format: https://dev.azure.com/org/project/_git/repo
            project = urllib.parse.quote(self.ado_project, safe='')
            return f"https://{host}/{self.ado_organization}/{project}/_git/{self.ado_repo}"
        else:
            # GitHub format: https://github.com/owner/repo
            return f"https://{host}/{self.repo_url}"
    
    def to_clone_url(self) -> str:
        """Convert to a clone-friendly URL (same as to_github_url for most purposes)."""
        return self.to_github_url()

    def get_display_name(self) -> str:
        """Get display name for this dependency (alias or repo name)."""
        if self.alias:
            return self.alias
        if self.is_virtual:
            return self.get_virtual_package_name()
        return self.repo_url  # Full repo URL for disambiguation

    def __str__(self) -> str:
        """String representation of the dependency reference."""
        if self.host:
            result = f"{self.host}/{self.repo_url}"
        else:
            result = self.repo_url
        if self.virtual_path:
            result += f"/{self.virtual_path}"
        if self.reference:
            result += f"#{self.reference}"
        if self.alias:
            result += f"@{self.alias}"
        return result


@dataclass
class MCPDependency:
    """Represents an MCP server dependency with optional overlay configuration.

    Supports three forms:
    - String (registry reference): MCPDependency.from_string("io.github.github/github-mcp-server")
    - Object with overlays: MCPDependency.from_dict({"name": "...", "transport": "stdio", ...})
    - Self-defined (registry: false): MCPDependency.from_dict({"name": "...", "registry": False, "transport": "http", "url": "..."})
    """
    name: str
    transport: Optional[str] = None          # "stdio" | "sse" | "streamable-http" | "http"
    env: Optional[Dict[str, str]] = None     # Environment variable overrides
    args: Optional[Any] = None               # Dict for overlay variable overrides, List for self-defined positional args
    version: Optional[str] = None            # Pin specific server version
    registry: Optional[Any] = None           # None=default, False=self-defined, str=custom registry URL
    package: Optional[str] = None            # "npm" | "pypi" | "oci" — select package type
    headers: Optional[Dict[str, str]] = None # Custom HTTP headers for remote endpoints
    tools: Optional[List[str]] = None        # Restrict exposed tools (default is ["*"])
    url: Optional[str] = None                # Required for self-defined http/sse transports
    command: Optional[str] = None            # Required for self-defined stdio transports

    @classmethod
    def from_string(cls, s: str) -> "MCPDependency":
        """Create an MCPDependency from a plain string (registry reference)."""
        return cls(name=s)

    @classmethod
    def from_dict(cls, d: dict) -> "MCPDependency":
        """Parse an MCPDependency from a dict.

        Handles backward compatibility: 'type' key is mapped to 'transport'.
        Unknown keys are silently ignored for forward compatibility.
        """
        if 'name' not in d:
            raise ValueError("MCP dependency dict must contain 'name'")

        transport = d.get('transport') or d.get('type')  # legacy 'type' -> 'transport'

        instance = cls(
            name=d['name'],
            transport=transport,
            env=d.get('env'),
            args=d.get('args'),
            version=d.get('version'),
            registry=d.get('registry'),
            package=d.get('package'),
            headers=d.get('headers'),
            tools=d.get('tools'),
            url=d.get('url'),
            command=d.get('command'),
        )

        if instance.registry is False:
            instance.validate()

        return instance

    @property
    def is_registry_resolved(self) -> bool:
        """True when the dependency is resolved via a registry."""
        return self.registry is not False

    @property
    def is_self_defined(self) -> bool:
        """True when the dependency is self-defined (registry: false)."""
        return self.registry is False

    def to_dict(self) -> dict:
        """Serialize to dict, including only non-None fields."""
        result: Dict[str, Any] = {'name': self.name}
        for field_name in ('transport', 'env', 'args', 'version', 'registry',
                           'package', 'headers', 'tools', 'url', 'command'):
            value = getattr(self, field_name)
            if value is not None or (field_name == 'registry' and value is False):
                result[field_name] = value
        return result

    _VALID_TRANSPORTS = frozenset({"stdio", "sse", "http", "streamable-http"})

    def __str__(self) -> str:
        """Return a redacted, human-friendly identifier for logging and CLI output."""
        if self.transport:
            return f"{self.name} ({self.transport})"
        return self.name

    def __repr__(self) -> str:
        """Return a redacted representation to keep secrets out of debug logs."""
        parts = [f"name={self.name!r}"]
        if self.transport:
            parts.append(f"transport={self.transport!r}")
        if self.env:
            safe_env = {k: '***' for k in self.env}
            parts.append(f"env={safe_env}")
        if self.headers:
            safe_headers = {k: '***' for k in self.headers}
            parts.append(f"headers={safe_headers}")
        if self.args is not None:
            parts.append("args=...")
        if self.tools:
            parts.append(f"tools={self.tools!r}")
        if self.url:
            parts.append(f"url={self.url!r}")
        if self.command:
            parts.append(f"command={self.command!r}")
        return f"MCPDependency({', '.join(parts)})"

    def validate(self) -> None:
        """Validate the dependency. Raises ValueError on invalid state."""
        if not self.name:
            raise ValueError("MCP dependency 'name' must not be empty")
        if self.transport and self.transport not in self._VALID_TRANSPORTS:
            raise ValueError(
                f"MCP dependency '{self.name}' has unsupported transport "
                f"'{self.transport}'. Valid values: {', '.join(sorted(self._VALID_TRANSPORTS))}"
            )
        if self.registry is False:
            if not self.transport:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' requires 'transport'"
                )
            if self.transport in ('http', 'sse', 'streamable-http') and not self.url:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' with transport "
                    f"'{self.transport}' requires 'url'"
                )
            if self.transport == 'stdio' and not self.command:
                raise ValueError(
                    f"Self-defined MCP dependency '{self.name}' with transport "
                    f"'stdio' requires 'command'"
                )


@dataclass
class APMPackage:
    """Represents an APM package with metadata."""
    name: str
    version: str
    description: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    source: Optional[str] = None  # Source location (for dependencies)
    resolved_commit: Optional[str] = None  # Resolved commit SHA (for dependencies)
    dependencies: Optional[Dict[str, List[Union[DependencyReference, str, dict]]]] = None  # Mixed types for APM/MCP/inline
    scripts: Optional[Dict[str, str]] = None
    package_path: Optional[Path] = None  # Local path to package
    target: Optional[str] = None  # Target agent: vscode, claude, or all (applies to compile and install)
    type: Optional[PackageContentType] = None  # Package content type: instructions, skill, hybrid, or prompts
    
    @classmethod
    def from_apm_yml(cls, apm_yml_path: Path) -> "APMPackage":
        """Load APM package from apm.yml file.
        
        Results are cached by resolved path for the lifetime of the process.
        
        Args:
            apm_yml_path: Path to the apm.yml file
            
        Returns:
            APMPackage: Loaded package instance
            
        Raises:
            ValueError: If the file is invalid or missing required fields
            FileNotFoundError: If the file doesn't exist
        """
        if not apm_yml_path.exists():
            raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")
        
        resolved = apm_yml_path.resolve()
        cached = _apm_yml_cache.get(resolved)
        if cached is not None:
            return cached
        
        try:
            with open(apm_yml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {apm_yml_path}: {e}")
        
        if not isinstance(data, dict):
            raise ValueError(f"apm.yml must contain a YAML object, got {type(data)}")
        
        # Required fields
        if 'name' not in data:
            raise ValueError("Missing required field 'name' in apm.yml")
        if 'version' not in data:
            raise ValueError("Missing required field 'version' in apm.yml")
        
        # Parse dependencies
        dependencies = None
        if 'dependencies' in data and isinstance(data['dependencies'], dict):
            dependencies = {}
            for dep_type, dep_list in data['dependencies'].items():
                if isinstance(dep_list, list):
                    if dep_type == 'apm':
                        # APM dependencies need to be parsed as DependencyReference objects
                        parsed_deps = []
                        for dep_entry in dep_list:
                            if isinstance(dep_entry, str):
                                try:
                                    parsed_deps.append(DependencyReference.parse(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid APM dependency '{dep_entry}': {e}")
                            elif isinstance(dep_entry, dict):
                                try:
                                    parsed_deps.append(DependencyReference.parse_from_dict(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid APM dependency {dep_entry}: {e}")
                        dependencies[dep_type] = parsed_deps
                    elif dep_type == 'mcp':
                        parsed_mcp = []
                        for dep in dep_list:
                            if isinstance(dep, str):
                                parsed_mcp.append(MCPDependency.from_string(dep))
                            elif isinstance(dep, dict):
                                try:
                                    parsed_mcp.append(MCPDependency.from_dict(dep))
                                except ValueError as e:
                                    raise ValueError(f"Invalid MCP dependency: {e}")
                        dependencies[dep_type] = parsed_mcp
                    else:
                        # Other dependency types: keep as-is
                        dependencies[dep_type] = [dep for dep in dep_list if isinstance(dep, (str, dict))]
        
        # Parse package content type
        pkg_type = None
        if 'type' in data and data['type'] is not None:
            type_value = data['type']
            if not isinstance(type_value, str):
                raise ValueError(f"Invalid 'type' field: expected string, got {type(type_value).__name__}")
            try:
                pkg_type = PackageContentType.from_string(type_value)
            except ValueError as e:
                raise ValueError(f"Invalid 'type' field in apm.yml: {e}")
        
        result = cls(
            name=data['name'],
            version=data['version'],
            description=data.get('description'),
            author=data.get('author'),
            license=data.get('license'),
            dependencies=dependencies,
            scripts=data.get('scripts'),
            package_path=apm_yml_path.parent,
            target=data.get('target'),
            type=pkg_type,
        )
        _apm_yml_cache[resolved] = result
        return result
    
    def get_apm_dependencies(self) -> List[DependencyReference]:
        """Get list of APM dependencies."""
        if not self.dependencies or 'apm' not in self.dependencies:
            return []
        # Filter to only return DependencyReference objects
        return [dep for dep in self.dependencies['apm'] if isinstance(dep, DependencyReference)]
    
    def get_mcp_dependencies(self) -> List["MCPDependency"]:
        """Get list of MCP dependencies."""
        if not self.dependencies or 'mcp' not in self.dependencies:
            return []
        return [dep for dep in (self.dependencies.get('mcp') or [])
                if isinstance(dep, MCPDependency)]
    
    def has_apm_dependencies(self) -> bool:
        """Check if this package has APM dependencies."""
        return bool(self.get_apm_dependencies())


@dataclass
class ValidationResult:
    """Result of APM package validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    package: Optional[APMPackage] = None
    package_type: Optional[PackageType] = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID
    
    def __init__(self):
        self.is_valid = True
        self.errors = []
        self.warnings = []
        self.package = None
        self.package_type = None
    
    def add_error(self, error: str) -> None:
        """Add a validation error."""
        self.errors.append(error)
        self.is_valid = False
    
    def add_warning(self, warning: str) -> None:
        """Add a validation warning."""
        self.warnings.append(warning)
    
    def has_issues(self) -> bool:
        """Check if there are any errors or warnings."""
        return bool(self.errors or self.warnings)
    
    def summary(self) -> str:
        """Get a summary of validation results."""
        if self.is_valid and not self.warnings:
            return "✅ Package is valid"
        elif self.is_valid and self.warnings:
            return f"⚠️ Package is valid with {len(self.warnings)} warning(s)"
        else:
            return f"❌ Package is invalid with {len(self.errors)} error(s)"


@dataclass
class PackageInfo:
    """Information about a downloaded/installed package."""
    package: APMPackage
    install_path: Path
    resolved_reference: Optional[ResolvedReference] = None
    installed_at: Optional[str] = None  # ISO timestamp
    dependency_ref: Optional["DependencyReference"] = None  # Original dependency reference for canonical string
    package_type: Optional[PackageType] = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID
    
    def get_canonical_dependency_string(self) -> str:
        """Get the canonical dependency string for this package.
        
        Used for orphan detection - this is the unique identifier as stored in apm.yml.
        For virtual packages, includes the full path (e.g., owner/repo/collections/name).
        For regular packages, just the repo URL (e.g., owner/repo).
        
        Returns:
            str: Canonical dependency string, or package source/name as fallback
        """
        if self.dependency_ref:
            return self.dependency_ref.get_canonical_dependency_string()
        # Fallback to package source or name
        return self.package.source or self.package.name or "unknown"
    
    def get_primitives_path(self) -> Path:
        """Get path to the .apm directory for this package."""
        return self.install_path / ".apm"
    
    def has_primitives(self) -> bool:
        """Check if the package has any primitives."""
        apm_dir = self.get_primitives_path()
        if apm_dir.exists():
            # Check for any primitive files in .apm/ subdirectories
            for primitive_type in ['instructions', 'chatmodes', 'contexts', 'prompts', 'hooks']:
                primitive_dir = apm_dir / primitive_type
                if primitive_dir.exists() and any(primitive_dir.iterdir()):
                    return True
        
        # Also check hooks/ at package root (Claude-native convention)
        hooks_dir = self.install_path / "hooks"
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            return True
        
        return False


def _has_hook_json(package_path: Path) -> bool:
    """Check if the package has hook JSON files in hooks/ or .apm/hooks/."""
    for hooks_dir in [package_path / "hooks", package_path / ".apm" / "hooks"]:
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            return True
    return False


def validate_apm_package(package_path: Path) -> ValidationResult:
    """Validate that a directory contains a valid APM package or Claude Skill.
    
    Supports four package types:
    - APM_PACKAGE: Has apm.yml and .apm/ directory
    - CLAUDE_SKILL: Has SKILL.md but no apm.yml (auto-generates apm.yml)
    - HOOK_PACKAGE: Has hooks/*.json but no apm.yml or SKILL.md
    - MARKETPLACE_PLUGIN: Has plugin.json but no apm.yml (synthesizes apm.yml)
    - HYBRID: Has both apm.yml and SKILL.md
    
    Args:
        package_path: Path to the directory to validate
        
    Returns:
        ValidationResult: Validation results with any errors/warnings
    """
    result = ValidationResult()
    
    # Check if directory exists
    if not package_path.exists():
        result.add_error(f"Package directory does not exist: {package_path}")
        return result
    
    if not package_path.is_dir():
        result.add_error(f"Package path is not a directory: {package_path}")
        return result
    
    # Detect package type
    apm_yml_path = package_path / "apm.yml"
    skill_md_path = package_path / "SKILL.md"

    # Check for plugin.json — optional metadata, not a detection gate
    from ..utils.helpers import find_plugin_json
    plugin_json_path = find_plugin_json(package_path)

    has_apm_yml = apm_yml_path.exists()
    has_skill_md = skill_md_path.exists()
    has_hooks = _has_hook_json(package_path)

    # Determine package type.  apm.yml / SKILL.md take precedence; everything
    # else (hooks-only or bare plugin directories) normalizes as a Claude plugin.
    if has_apm_yml and has_skill_md:
        result.package_type = PackageType.HYBRID
    elif has_apm_yml:
        result.package_type = PackageType.APM_PACKAGE
    elif has_skill_md:
        result.package_type = PackageType.CLAUDE_SKILL
    elif has_hooks:
        result.package_type = PackageType.HOOK_PACKAGE
    else:
        # Fallback: treat any directory without apm.yml / SKILL.md as a Claude plugin.
        # plugin.json, when present, is read as optional metadata.
        result.package_type = PackageType.MARKETPLACE_PLUGIN
    
    # Handle hook-only packages (no apm.yml or SKILL.md)
    if result.package_type == PackageType.HOOK_PACKAGE:
        return _validate_hook_package(package_path, result)
    
    # Handle Claude Skills (no apm.yml) - auto-generate minimal apm.yml
    if result.package_type == PackageType.CLAUDE_SKILL:
        return _validate_claude_skill(package_path, skill_md_path, result)
    
    # Handle Marketplace Plugins (no apm.yml) - synthesize apm.yml from plugin.json
    if result.package_type == PackageType.MARKETPLACE_PLUGIN:
        return _validate_marketplace_plugin(package_path, plugin_json_path, result)
    
    # Standard APM package validation (has apm.yml)
    return _validate_apm_package_with_yml(package_path, apm_yml_path, result)


def _validate_hook_package(package_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a hook-only package and create APMPackage from its metadata.
    
    A hook package has hooks/*.json (or .apm/hooks/*.json) defining hook
    handlers per the Claude Code hooks specification, but no apm.yml or SKILL.md.
    
    Args:
        package_path: Path to the package directory  
        result: ValidationResult to populate
        
    Returns:
        ValidationResult: Updated validation result
    """
    package_name = package_path.name
    
    # Create APMPackage from directory name
    package = APMPackage(
        name=package_name,
        version="1.0.0",
        description=f"Hook package: {package_name}",
        package_path=package_path,
        type=PackageContentType.HYBRID
    )
    result.package = package
    
    return result


def _validate_claude_skill(package_path: Path, skill_md_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a Claude Skill and create APMPackage directly from SKILL.md metadata.
    
    Args:
        package_path: Path to the package directory
        skill_md_path: Path to SKILL.md
        result: ValidationResult to populate
        
    Returns:
        ValidationResult: Updated validation result
    """
    import frontmatter
    
    try:
        # Parse SKILL.md to extract metadata
        with open(skill_md_path, 'r', encoding='utf-8') as f:
            post = frontmatter.load(f)
        
        skill_name = post.metadata.get('name', package_path.name)
        skill_description = post.metadata.get('description', f"Claude Skill: {skill_name}")
        skill_license = post.metadata.get('license')
        
        # Create APMPackage directly from SKILL.md metadata - no file generation needed
        package = APMPackage(
            name=skill_name,
            version="1.0.0",
            description=skill_description,
            license=skill_license,
            package_path=package_path,
            type=PackageContentType.SKILL
        )
        result.package = package
        
    except Exception as e:
        result.add_error(f"Failed to process SKILL.md: {e}")
        return result
    
    return result


def _validate_marketplace_plugin(package_path: Path, plugin_json_path: Optional[Path], result: ValidationResult) -> ValidationResult:
    """Validate a Claude plugin and synthesize apm.yml.

    plugin.json is **optional** per the spec.  When present it provides
    metadata (name, version, description …).  When absent the plugin name is
    derived from the directory name and all other fields default gracefully.

    Args:
        package_path: Path to the package directory
        plugin_json_path: Path to plugin.json if found, or None
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result with MARKETPLACE_PLUGIN type
    """
    from ..deps.plugin_parser import normalize_plugin_directory

    try:
        # Normalize the plugin directory; plugin.json is optional metadata
        apm_yml_path = normalize_plugin_directory(package_path, plugin_json_path)

        # Load the synthesized apm.yml
        package = APMPackage.from_apm_yml(apm_yml_path)
        result.package = package
        result.package_type = PackageType.MARKETPLACE_PLUGIN

    except Exception as e:
        result.add_error(f"Failed to process Claude plugin: {e}")
        return result

    return result


def _validate_apm_package_with_yml(package_path: Path, apm_yml_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a standard APM package with apm.yml.
    
    Args:
        package_path: Path to the package directory
        apm_yml_path: Path to apm.yml
        result: ValidationResult to populate
        
    Returns:
        ValidationResult: Updated validation result
    """
    # Try to parse apm.yml
    try:
        package = APMPackage.from_apm_yml(apm_yml_path)
        result.package = package
    except (ValueError, FileNotFoundError) as e:
        result.add_error(f"Invalid apm.yml: {e}")
        return result
    
    # Check for .apm directory
    apm_dir = package_path / ".apm"
    if not apm_dir.exists():
        result.add_error("Missing required directory: .apm/")
        return result
    
    if not apm_dir.is_dir():
        result.add_error(".apm must be a directory")
        return result
    
    # Check if .apm directory has any content
    primitive_types = ['instructions', 'chatmodes', 'contexts', 'prompts']
    has_primitives = False
    
    for primitive_type in primitive_types:
        primitive_dir = apm_dir / primitive_type
        if primitive_dir.exists() and primitive_dir.is_dir():
            # Check if directory has any markdown files
            md_files = list(primitive_dir.glob("*.md"))
            if md_files:
                has_primitives = True
                # Validate each primitive file has basic structure
                for md_file in md_files:
                    try:
                        content = md_file.read_text(encoding='utf-8')
                        if not content.strip():
                            result.add_warning(f"Empty primitive file: {md_file.relative_to(package_path)}")
                    except Exception as e:
                        result.add_warning(f"Could not read primitive file {md_file.relative_to(package_path)}: {e}")
    
    # Also check for hooks (JSON files in .apm/hooks/ or hooks/)
    if not has_primitives:
        has_primitives = _has_hook_json(package_path)
    
    if not has_primitives:
        result.add_warning("No primitive files found in .apm/ directory")
    
    # Version format validation (basic semver check)
    if package and package.version is not None:
        # Defensive cast in case YAML parsed a numeric like 1 or 1.0 
        version_str = str(package.version).strip()
        if not re.match(r'^\d+\.\d+\.\d+', version_str):
            result.add_warning(f"Version '{version_str}' doesn't follow semantic versioning (x.y.z)")
    
    return result


def parse_git_reference(ref_string: str) -> tuple[GitReferenceType, str]:
    """Parse a git reference string to determine its type.
    
    Args:
        ref_string: Git reference (branch, tag, or commit)
        
    Returns:
        tuple: (GitReferenceType, cleaned_reference)
    """
    if not ref_string:
        return GitReferenceType.BRANCH, "main"  # Default to main branch
    
    ref = ref_string.strip()
    
    # Check if it looks like a commit SHA (40 hex chars or 7+ hex chars)
    if re.match(r'^[a-f0-9]{7,40}$', ref.lower()):
        return GitReferenceType.COMMIT, ref
    
    # Check if it looks like a semantic version tag
    if re.match(r'^v?\d+\.\d+\.\d+', ref):
        return GitReferenceType.TAG, ref
    
    # Otherwise assume it's a branch
    return GitReferenceType.BRANCH, ref