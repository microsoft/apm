"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import (
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
    validate_ssh_user,
)
from ...utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ..validation import InvalidVirtualPackageExtensionError
from .identity import (
    _NON_ADO_PATH_SEGMENT_RE,
    InvalidSemverRangeError,
    _is_valid_registry_semver_range,
    _looks_like_invalid_semver_range,
    _path_segment_pattern,
    build_canonical_dependency_string,
    build_dependency_unique_key,
    normalize_package_repo_url,
)
from .object_fields import (
    apply_optional_dependency_fields,
    local_path_apm_yml_entry,
    parse_alias_override,
    reject_unknown_fields,
)
from .types import VirtualPackageType

# Identity/semver helpers re-exported from .identity for back-compat imports.
# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}

_REF_VERSION_SUFFIX_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][A-Za-z0-9][A-Za-z0-9._-]*)?$")


@dataclass
class DependencyReference:
    """Represents a reference to an APM dependency."""

    repo_url: str  # e.g., "user/repo" for GitHub or "org/project/repo" for Azure DevOps
    host: str | None = None  # Optional host (github.com, dev.azure.com, or enterprise host)
    host_type: str | None = None  # Explicit host kind override (currently: "gitlab")
    port: int | None = None  # Non-standard SSH/HTTPS port (e.g. 7999 for Bitbucket DC)
    explicit_scheme: str | None = (
        None  # User-stated transport: "ssh", "https", "http", or None for shorthand
    )
    reference: str | None = None  # e.g., "main", "v1.0.0", "abc123"
    alias: str | None = None  # Optional alias for the dependency
    virtual_path: str | None = None  # Path for virtual packages (e.g., "prompts/file.prompt.md")
    is_virtual: bool = False  # True if this is a virtual package (individual file or subdirectory)

    # Azure DevOps specific fields (ADO uses org/project/repo structure)
    ado_organization: str | None = None  # e.g., "dmeppiel-org"
    ado_project: str | None = None  # e.g., "market-js-app"
    ado_repo: str | None = None  # e.g., "compliance-rules"

    # Local path dependency fields
    is_local: bool = False  # True if this is a local filesystem dependency
    local_path: str | None = None  # Original local path string (e.g., "./packages/my-pkg")
    declaring_parent: str | None = None
    anchored_local_path: str | None = None

    # Monorepo inheritance: { git: parent, path: ... } — expanded in resolver
    is_parent_repo_inheritance: bool = False

    artifactory_prefix: str | None = None  # e.g., "artifactory/github" (repo key path)

    # HTTP (insecure) dependency fields
    is_insecure: bool = False  # True when the dependency URL uses http://
    allow_insecure: bool = False  # True if this HTTP dep is explicitly allowed

    # SKILL_BUNDLE subset selection (persisted in apm.yml `skills:` field)
    skill_subset: list[str] | None = None  # Sorted skill names, or None = all
    target_subset: list[str] | None = None  # Sorted lowercase target names, or None = all

    # SSH username for SCP-shorthand or ``ssh://`` dependencies. ``None`` for
    # non-SSH inputs. Defaults to ``"git"`` whenever an SSH form was parsed
    # without an explicit user. Carried as auth/transport context, NOT
    # baked into ``to_canonical()`` / ``get_identity()`` so dependency
    # identity stays user-agnostic (lockfile pinning + dedup work the same
    # whether a project uses ``git@`` or an EMU/custom SSH account).
    ssh_user: str | None = None

    # Registry resolver fields (optional; default to None/git semantics)
    # source: which resolver should fetch this dep. None and "git" are equivalent
    # (legacy default). Set to "registry" by the parser when an entry routes to
    # a configured registry (via top-level registries: block or
    # object-form `- registry:` / `- id:` discriminator), and to "local" when
    # the entry is a local filesystem path (is_local=True) so every reader and
    # the lockfile (which records source="local") agree on a local dep's source.
    # registry_name: name of the registry from apm.yml's registries: block when
    # source == "registry". Carried in-memory only; never serialized into the
    # lockfile (the lockfile uses URL-based identity per design §6.1).
    source: str | None = None
    registry_name: str | None = None

    # Marketplace dependency fields (parsed from plugin.json dict format)
    is_marketplace: bool = False
    marketplace_name: str | None = None
    marketplace_plugin_name: str | None = None
    marketplace_version_spec: str | None = None

    def __post_init__(self) -> None:
        """Normalize case-insensitive package identity at the model boundary."""
        self.repo_url = normalize_package_repo_url(
            self.repo_url,
            host=self.host,
            source=self.source,
            registry_prefix=self.artifactory_prefix,
            is_local=self.is_local,
            is_marketplace=self.is_marketplace,
        )

    @property
    def ref_kind(self) -> str | None:
        """Classify ``reference`` for routing purposes.

        Returns one of:

        * ``"semver"`` -- ``reference`` parses as a valid semver range
          (``^1.2.0``, ``~2.1``, ``>=1.0 <2.0``, ``1.2.x``, exact ``1.2.3``).
          The install pipeline resolves it against the remote's tags via
          :class:`~apm_cli.deps.git_semver_resolver.GitSemverResolver`.
        * ``"literal"`` -- ``reference`` is a non-empty string that does
          NOT parse as semver (branch name, tag name with prefix, SHA).
        * ``None`` -- ``reference`` is unset; downstream uses the remote's
          default branch.

        Semver routing is opt-in by syntax: any ``ref:`` value that
        survives the literal-branch / literal-tag / SHA parse intact
        bypasses the semver resolver, so existing dependencies on
        ``ref: v1.2.3`` (literal tag with ``v`` prefix) keep their
        existing behaviour.

        Note: ``"1.2.3"`` (no ``v`` prefix) parses as a semver exact-version
        constraint, NOT a literal tag.  The git-semver resolver's bare-
        version fallback pattern covers the "literal ``1.2.3`` tag on the
        remote" case without breaking semver routing for the same input.
        """
        if not self.reference:
            return None
        # ``v1.2.3``, ``main``, SHAs, anything-with-prefix is literal.
        # Only inputs that parse as a *standalone* semver range are
        # routed through the git-semver resolver.
        if _is_valid_registry_semver_range(self.reference):
            return "semver"
        if _looks_like_invalid_semver_range(self.reference):
            raise InvalidSemverRangeError(
                f"Invalid semver range in ref {self.reference!r}. "
                "The ref field expects a plain semver range. "
                "Use a range like '^1.2.0' or pin a literal tag like "
                "'pkg-a-v1.2.0'."
            )
        return "literal"

    # Supported file extensions for virtual packages
    VIRTUAL_FILE_EXTENSIONS = (
        ".prompt.md",
        ".instructions.md",
        ".agent.md",
    )

    # Removed collection-manifest extensions. URLs ending in one of these are
    # rejected at parse time with a migration message; the legacy
    # `.collection.yml` curated-aggregator format is replaced by `apm.yml`
    # with a `dependencies` section (#1094).
    REMOVED_COLLECTION_EXTENSIONS = (
        ".collection.yml",
        ".collection.yaml",
    )

    # First path segment after host that often starts in-repo virtual layout (GitLab heuristic).
    _GITLAB_VIRTUAL_ROOT_SEGMENTS = frozenset({"prompts", "instructions", "collections"})

    # Known APM primitive directory names. Used to detect a subpath accidentally
    # embedded inside an explicit git URL form (SCP/ssh://https://), which git
    # would later reject with a cryptic "not a valid repository name" error.
    _APM_PRIMITIVE_DIRS: frozenset[str] = frozenset(
        {
            "skills",
            "agents",
            "prompts",
            "instructions",
            "collections",
            "contexts",
            "memory",
        }
    )

    def is_artifactory(self) -> bool:
        """Check if this reference points to a JFrog Artifactory VCS repository."""
        return self.artifactory_prefix is not None

    def is_azure_devops(self) -> bool:
        """Check if this reference points to Azure DevOps."""
        from ...utils.github_host import is_azure_devops_hostname

        return self.host is not None and is_azure_devops_hostname(self.host)

    @property
    def virtual_type(self) -> "VirtualPackageType | None":
        """Return the type of virtual package, or None if not virtual.

        Classification is by extension only -- never by path segment.
        ``.prompt.md``/``.instructions.md``/``.agent.md``
        is FILE; everything else is SUBDIRECTORY (resolved at fetch time
        by probing for ``apm.yml``, ``SKILL.md``, ``plugin.json``, etc).
        Paths like ``collections/foo`` (no extension) are SUBDIRECTORY.
        """
        if not self.is_virtual or not self.virtual_path:
            return None
        if any(self.virtual_path.endswith(ext) for ext in self.VIRTUAL_FILE_EXTENSIONS):
            return VirtualPackageType.FILE
        return VirtualPackageType.SUBDIRECTORY

    def is_virtual_file(self) -> bool:
        """Check if this is a virtual file package (individual file)."""
        return self.virtual_type == VirtualPackageType.FILE

    def is_virtual_subdirectory(self) -> bool:
        """Check if this is a virtual subdirectory package (e.g., Claude Skill).

        A subdirectory package is a virtual package whose ``virtual_path``
        does not end in a recognized FILE extension. The actual on-disk
        shape is resolved at fetch time -- ``apm.yml``, ``SKILL.md``,
        ``plugin.json``, etc.

        Examples:
            - ComposioHQ/awesome-claude-skills/brand-guidelines -> True
            - owner/repo/prompts/file.prompt.md -> False (is_virtual_file)
            - owner/repo/collections/name -> True (resolved at fetch time)
        """
        return self.virtual_type == VirtualPackageType.SUBDIRECTORY

    def get_virtual_package_name(self) -> str:
        """Generate a package name for this virtual package.

        For virtual packages, we create a sanitized name from the path:
        - owner/repo/prompts/code-review.prompt.md -> repo-code-review
        - owner/repo/collections/project-planning -> repo-project-planning
        """
        if not self.is_virtual or not self.virtual_path:
            return self.repo_url.split("/")[-1]  # Return repo name as fallback

        # Extract repo name and file/collection name
        repo_parts = self.repo_url.split("/")
        repo_name = repo_parts[-1] if repo_parts else "package"

        # Get the basename without extension
        path_parts = self.virtual_path.split("/")
        last = path_parts[-1]
        # Strip any recognised virtual file extension. The directory name
        # (or file basename) is the user-visible package name.
        for ext in self.VIRTUAL_FILE_EXTENSIONS:
            if last.endswith(ext):
                last = last[: -len(ext)]
                break
        return f"{repo_name}-{last}"

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
        if s.startswith(("./", "../", "/", "~/", "~\\", ".\\", "..\\")):
            return True
        # Windows absolute paths: drive letter + colon + separator (C:\ or C:/).
        # Only ASCII letters A-Z/a-z are valid drive letters.
        return bool(
            len(s) >= 3
            and ("A" <= s[0] <= "Z" or "a" <= s[0] <= "z")
            and s[1] == ":"
            and s[2] in ("\\", "/")
        )

    def get_unique_key(self) -> str:
        """Get a unique key for this dependency for deduplication.

        For regular packages: repo_url
        For virtual packages: repo_url + virtual_path to ensure uniqueness
        For local packages: the local_path

        Returns:
            str: Unique key for this dependency
        """
        return build_dependency_unique_key(
            self.repo_url,
            host=self.host,
            source="local" if self.is_local else self.source,
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
            registry_prefix=self.artifactory_prefix,
            declaring_parent=self.declaring_parent,
            anchored_local_path=self.anchored_local_path,
        )

    def get_resolution_key(self) -> str:
        """Return identity plus the declared ref constraint."""
        if self.reference:
            return f"{self.get_unique_key()}#{self.reference}"
        return self.get_unique_key()

    def get_cycle_key(self) -> str:
        """Return physical local identity for recursion detection."""
        if self.is_local and self.anchored_local_path:
            return f"local:{self.anchored_local_path}"
        return self.get_unique_key()

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
        For the lockfile dedup key (host-qualified for non-default hosts), use get_unique_key().

        Returns:
            str: Host-blind canonical string (e.g., "owner/repo")
        """
        return build_canonical_dependency_string(
            self.repo_url,
            is_local=self.is_local,
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
        )

    def get_install_path(self, apm_modules_dir: Path) -> Path:
        """Get the canonical filesystem path where this package should be installed.

        This is the single source of truth for where a package lives in apm_modules/.

        Args:
            apm_modules_dir: Path to the apm_modules directory

        Raises:
            ValueError: If this is an unresolved marketplace dependency
            PathTraversalError: If the computed path escapes apm_modules_dir
        Returns:
            Path: Absolute path to the package installation directory
        """
        if self.is_marketplace:
            raise ValueError(
                f"Cannot compute install path for unresolved marketplace dependency "
                f"'{self.marketplace_plugin_name}@{self.marketplace_name}'"
            )

        if self.is_local and self.local_path:
            pkg_dir_name = Path(self.local_path).name
            validate_path_segments(
                pkg_dir_name,
                context="local package path",
                reject_empty=True,
            )
            if self.declaring_parent:
                import hashlib

                identity = self.anchored_local_path or self.local_path
                parent_slot = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
                result = apm_modules_dir / "_local" / parent_slot / pkg_dir_name
            else:
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
                    result = apm_modules_dir / repo_parts[0] / repo_parts[1] / package_name
                elif len(repo_parts) >= 2:
                    # owner/virtual-pkg-name (use first segment as namespace)
                    result = apm_modules_dir / repo_parts[0] / package_name
        # Regular package: use full repo path
        elif self.is_azure_devops() and len(repo_parts) >= 3:
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
    def _reject_shorthand_alias(dependency_str: str) -> None:
        """Reject bare-shorthand ``@alias`` with an actionable migration error.

        Bare ``@alias`` is not part of the supported reference grammar (#340
        retired the ``@`` separator to avoid the npm/go/cargo ``@version``
        collision). The dedicated SSH parsers handle ``@`` in ``ssh://`` URLs
        and SCP shorthand (``<user>@host:path``) as userinfo, not aliases; this
        guard rejects ``@`` in the pre-fragment shorthand portion and keeps the
        retired ``#ref@alias`` shape rejected, while version-style tag suffixes
        such as ``owner/repo#package@v1.0.1`` remain valid literal refs.
        """
        stripped = dependency_str.strip()
        if "@" not in stripped:
            return
        if stripped.lower().startswith(("https://", "http://", "ssh://")):
            return
        if SCP_LIKE_RE.match(stripped):
            return
        shorthand_part, _, ref_part = stripped.partition("#")
        if "@" not in shorthand_part:
            _, _, ref_suffix = ref_part.rpartition("@")
            if _REF_VERSION_SUFFIX_RE.fullmatch(ref_suffix):
                return
        preview = "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in stripped)
        if len(preview) > 160:
            preview = f"{preview[:157]}..."
        raise ValueError(
            f"Shorthand '@alias' is not supported in '{preview}'. "
            f"Use object form with 'git:', optional 'path:', and 'alias:' "
            f"fields to install a dependency under a custom directory name. "
            f"See: https://microsoft.github.io/apm/consumer/manage-dependencies/#reference-formats"
        )

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
            ``(host, port, repo_url, reference, alias, user)`` or ``None`` if
            the input is not an ``ssh://`` URL. ``user`` defaults to ``"git"``
            when no userinfo is present.
        """
        if not url.startswith("ssh://"):
            return None

        # SECURITY: reject percent-encoded userinfo BEFORE urlparse decodes it.
        # ``urlparse('ssh://%2DoProxyCommand=evil@host/repo').username`` returns
        # ``-oProxyCommand=evil`` which would smuggle SSH options past the
        # allowlist in validate_ssh_user. We inspect the raw substring between
        # ``ssh://`` and the first ``@`` (which terminates the userinfo per
        # RFC 3986) and reject any ``%`` there. There is no legitimate need for
        # percent-encoding in a real SSH username.
        userinfo_match = re.match(r"^ssh://([^@/?#]+)@", url)
        if userinfo_match and "%" in userinfo_match.group(1):
            raise ValueError(
                "Percent-encoded characters are not allowed in SSH userinfo. "
                "Use the literal username (e.g. 'ssh://myuser@host/...')."
            )

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port  # int or None
        # Normalise default SSH port so ssh://host:22/... matches ssh://host/...
        if port == _DEFAULT_SCHEME_PORTS.get("ssh"):
            port = None
        path = parsed.path.lstrip("/")
        fragment = parsed.fragment

        # Userinfo: validate or default to "git". urlparse exposes ``username``
        # already percent-decoded; the pre-check above guarantees no decoding
        # actually happened, so what we see equals what was on the wire.
        raw_user = parsed.username
        ssh_user = validate_ssh_user(raw_user) if raw_user else "git"

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

        return host, port, repo_url, reference, alias, ssh_user

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
    def _check_no_embedded_subpath(url: str) -> None:
        """Guard: reject a subpath embedded in an explicit git URL form (#872).

        Detects when a user writes, e.g.:
            git: git@github.com:org/repo/skills/hello-world.git

        Such URLs cause git to fail later with a cryptic
        ``fatal: '...' does not appear to be a git repository`` message.
        This guard fires early and points at the supported ``path:`` key.

        The heuristic: for SCP (``git@host:path``), ``ssh://``, or
        ``https://``/``http://`` URL forms, if any non-last path segment
        matches a known APM primitive directory name (skills, agents, prompts,
        etc.), the URL encodes a subpath that belongs in the ``path:`` key.

        GitLab subgroups and Azure DevOps org/project paths do not use APM
        primitive names (skills, agents, prompts, ...) as segment labels, so
        the check produces no false positives for those legitimate forms.

        Scoping (issue #1014 follow-up): the embedded-subpath shape is
        ``org/repo`` followed by ``<primitive>/<name>``, so a primitive
        segment is only treated as an embedded subpath when it is preceded
        by a complete ``org/repo`` prefix (segment index >= 2). This avoids
        a false positive for a GitLab subgroup literally named after a
        primitive, e.g. ``git@gitlab.com:group/skills/repo.git`` (here
        ``skills`` is a subgroup at index 1 and ``repo`` is the real
        repository). A residual ambiguity remains for deep subgroups that
        embed a primitive name at index >= 2 (e.g.
        ``group/sub/skills/repo``); that shape is genuinely undecidable
        without probing the host, so it is still treated as malformed.
        """
        raw = url.strip()

        if SCP_LIKE_RE.match(raw):
            colon_idx = raw.index(":")
            path_part = raw[colon_idx + 1 :]
        elif raw.lower().startswith(("ssh://", "https://", "http://")):
            path_part = urllib.parse.urlparse(raw).path
        else:
            return  # bare shorthand or other form -- not in scope

        # Strip fragment and query string, then remove trailing .git suffix
        path_part = path_part.split("#")[0].split("?")[0]
        if path_part.endswith(".git"):
            path_part = path_part[:-4]

        segments = [s for s in path_part.replace("\\", "/").split("/") if s]
        if len(segments) < 3:
            return  # too few segments to contain an interior primitive name

        # Azure DevOps repo URLs carry the repository under a `_git` segment
        # and legitimately encode a virtual path after it (e.g.
        # dev.azure.com/org/proj/_git/repo/instructions/x). That is the
        # supported ADO shorthand, not an embedded subpath, so skip the guard
        # for any URL containing the ADO-specific `_git` marker (no GitHub or
        # GitLab repo path uses `_git`, so real detection is unaffected).
        if "_git" in segments:
            return

        # An embedded subpath is `org/repo` + `<primitive>/<name>`, so the
        # primitive directory must be preceded by a complete org/repo prefix
        # (index >= 2). Restricting to index >= 2 keeps the real malformed-URL
        # detection (org/repo/skills/<name>) while not false-positiving on a
        # subgroup literally named after a primitive at index 1
        # (group/skills/repo, where `repo` is the actual repository).
        for idx, seg in enumerate(segments[:-1]):
            if idx >= 2 and seg in DependencyReference._APM_PRIMITIVE_DIRS:
                raise ValueError(
                    "A subpath cannot be embedded in a git URL. "
                    f"Got: `{raw}`. "
                    "Use the `path:` key instead: "
                    "`git: <repo-url>` + `path: <primitive>/<name>` "
                    "(or the shorthand `org/repo/<primitive>/<name>`). "
                    "See https://microsoft.github.io/apm/consumer/manage-dependencies/"
                )

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

        And marketplace dependency entries:

            - name: gopls-lsp
              marketplace: claude-plugins-official

            - name: secrets-vault
              marketplace: acme-tools
              version: "~2.1.0"

        Args:
            entry: Dictionary with 'git', 'path', or 'marketplace' key.
                   Marketplace entries support 'name', 'marketplace', and
                   optional 'version' (semver range) fields.

        Returns:
            DependencyReference: Parsed dependency reference

        Raises:
            ValueError: If the entry is missing required fields or has invalid format
        """
        # Support marketplace dependencies: { name: X, marketplace: Y, version: Z }
        if "marketplace" in entry:
            source_keys = {"git", "path", "registry", "id"}.intersection(entry)
            if source_keys:
                joined = "', '".join(sorted(source_keys))
                raise ValueError(
                    f"Ambiguous dependency: 'marketplace' cannot be combined with '{joined}'"
                )
            _MARKETPLACE_KEYS = {"name", "marketplace", "version"}
            unknown = set(entry.keys()) - _MARKETPLACE_KEYS
            if unknown:
                raise ValueError(
                    f"Unknown keys in marketplace dependency: {sorted(unknown)}. "
                    f"Allowed keys: {sorted(_MARKETPLACE_KEYS)}"
                )
            name = entry.get("name")
            marketplace = entry["marketplace"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Marketplace dependency must have a non-empty 'name' field")
            if not isinstance(marketplace, str) or not marketplace.strip():
                raise ValueError("'marketplace' field must be a non-empty string")
            name = name.strip()
            marketplace = marketplace.strip()
            if not re.match(r"^[a-zA-Z0-9._-]+$", name):
                raise ValueError(
                    f"Invalid marketplace plugin name: '{name}'. "
                    "Names can only contain letters, numbers, dots, underscores, and hyphens"
                )
            if not re.match(r"^[a-zA-Z0-9._-]+$", marketplace):
                raise ValueError(
                    f"Invalid marketplace name: '{marketplace}'. "
                    "Names can only contain letters, numbers, dots, underscores, and hyphens"
                )
            version_spec = entry.get("version")
            if version_spec is not None:
                if not isinstance(version_spec, str) or not version_spec.strip():
                    raise ValueError("'version' field must be a non-empty string")
                version_spec = version_spec.strip()
            return cls(
                repo_url=f"_marketplace/{marketplace}/{name}",
                is_marketplace=True,
                marketplace_name=marketplace,
                marketplace_plugin_name=name,
                marketplace_version_spec=version_spec,
            )

        # Object-form registry package — design §3.2.
        # Discriminated by the ``registry:`` or ``id:`` key (``registry:`` is
        # optional when a ``registries.default:`` is configured).  Mutually
        # exclusive with ``git:``.
        if "registry" in entry or "id" in entry:
            if "git" in entry:
                raise ValueError(
                    "Object-style dependency cannot mix 'registry:'/'id:' and 'git:' "
                    "keys — choose one resolver."
                )
            return cls._parse_registry_object_entry(entry)

        # Support dict-form local path: { path: ./local/dir }
        if "path" in entry and "git" not in entry:
            reject_unknown_fields(entry, {"path", "alias", "skills", "targets"}, "path")
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
            dep = cls.parse(local)
            apply_optional_dependency_fields(dep, entry)
            return dep

        if "git" not in entry:
            raise ValueError(
                "Object-style dependency must have a 'git', 'path', or 'registry' field"
            )

        git_url = entry["git"]
        if not isinstance(git_url, str) or not git_url.strip():
            raise ValueError("'git' field must be a non-empty string")
        host_type = cls._parse_host_type(entry.get("type"))

        # Monorepo parent inheritance (literal ``git: parent`` only; resolver expands)
        if git_url == "parent":
            if host_type is not None:
                raise ValueError("'type' is only supported for remote git dependencies")
            path_raw = entry.get("path")
            if path_raw is None:
                raise ValueError(
                    "Object-style dependency with git: 'parent' requires a 'path' field"
                )
            if not isinstance(path_raw, str) or not path_raw.strip():
                raise ValueError("'path' field must be a non-empty string")
            normalized_path = cls._normalize_parent_repo_decl_path(path_raw)

            ref_override = entry.get("ref")
            reference: str | None = None
            if ref_override is not None:
                if not isinstance(ref_override, str) or not ref_override.strip():
                    raise ValueError("'ref' field must be a non-empty string")
                reference = ref_override.strip()

            return cls(
                repo_url="_parent",
                host=None,
                reference=reference,
                alias=parse_alias_override(entry.get("alias")),
                virtual_path=normalized_path,
                is_virtual=True,
                is_parent_repo_inheritance=True,
            )

        sub_path = entry.get("path")
        ref_override = entry.get("ref")
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
        dep.host_type = host_type
        dep.allow_insecure = allow_insecure
        # Object-form ``- git:`` is an explicit Git resolver pin, even when
        # a top-level ``registries.default`` is set. Mark source so the
        # default-routing pass in apm_package.py leaves it alone.
        dep.source = "git"

        # Apply overrides from the object fields
        if ref_override is not None:
            if not isinstance(ref_override, str) or not ref_override.strip():
                raise ValueError("'ref' field must be a non-empty string")
            dep.reference = ref_override.strip()

        # Apply sub-path as virtual package
        if sub_path:
            dep.virtual_path = sub_path
            dep.is_virtual = True

        apply_optional_dependency_fields(dep, entry)
        return dep

    @staticmethod
    def _parse_host_type(raw: object) -> str | None:
        """Parse the optional object-form ``type`` host-kind hint.

        Values come from the canonical host-provider registry. Unknown hints
        fail closed rather than silently selecting a generic transport.
        """
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("'type' field must be a non-empty string")
        value = raw.strip().lower()
        from apm_cli.core.host_providers import accepted_host_types

        supported = accepted_host_types()
        if value not in supported:
            raise ValueError(f"'type' field supports {', '.join(supported)}; got {raw!r}")
        return value

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
    def split_gitlab_direct_shorthand_parts(
        cls, package: str
    ) -> tuple[str, list[str], str | None] | None:
        """If *package* is bare host/path shorthand, return (host, path_segments, ref_str).

        Returns ``None`` for ``https://``, ``git@``, or non-GitLab-class hosts.
        """
        s = package.strip()
        ref_out: str | None = None
        if "#" in s:
            s, r = s.rsplit("#", 1)
            s = s.strip()
            r = r.strip()
            ref_out = r if r else None
        maybe_raise_bare_fqdn_github_gitlab_conflict(package)
        if s.startswith(("git@", "https://", "http://", "ssh://", "//")):
            return None
        if "/" not in s:
            return None
        parts = s.split("/")
        host_cand = parts[0]
        if "." not in host_cand:
            return None
        segs = [p for p in parts[1:] if p]
        if len(segs) < 1:
            return None
        if not is_supported_git_host(host_cand) or not is_gitlab_hostname(host_cand):
            return None
        return (host_cand, segs, ref_out)

    @classmethod
    def needs_gitlab_direct_shorthand_probing(
        cls, package: str, dep_ref: "DependencyReference"
    ) -> bool:
        """True when install should probe left-to-right repo boundaries (GitLab only)."""
        if dep_ref.is_local:
            return False
        if dep_ref.is_virtual:
            return False
        sp = cls.split_gitlab_direct_shorthand_parts(package)
        if not sp:
            return False
        _host, segs, _ref = sp
        return len(segs) >= 3

    @classmethod
    def iter_gitlab_direct_shorthand_boundary_candidates(cls, path_segments: list[str]):
        """Yield (repo_url, virtual_suffix) for k=2..n-1 (earliest k first)."""
        n = len(path_segments)
        if n < 3:
            return
        for k in range(2, n):
            repo = "/".join(path_segments[:k])
            suffix = "/".join(path_segments[k:])
            if cls.virtual_suffix_is_installable_shape(suffix):
                yield repo, suffix

    @classmethod
    def from_gitlab_shorthand_probe(
        cls,
        host: str,
        repo_url: str,
        virtual_path: str,
        reference: str | None,
    ) -> "DependencyReference":
        """Build a virtual dependency ref for a resolved GitLab shorthand probe."""
        return cls(
            repo_url=repo_url,
            host=host,
            reference=reference,
            virtual_path=virtual_path,
            is_virtual=True,
        )

    @classmethod
    def from_artifactory_boundary_probe(
        cls,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        virtual_path: str | None,
        reference: str | None,
    ) -> "DependencyReference":
        """Build a dependency ref for a resolved Artifactory boundary probe."""
        return cls(
            repo_url=f"{owner}/{repo}",
            host=host,
            reference=reference,
            virtual_path=virtual_path,
            is_virtual=bool(virtual_path),
            artifactory_prefix=prefix,
        )

    @classmethod
    def _gitlab_shorthand_repo_segment_count(
        cls,
        path_segments: list[str],
        has_virtual_ext: bool,
        has_collection: bool,
    ) -> int:
        """Return how many segments after the host belong to the GitLab project path.

        GitLab allows nested groups; unlike GitHub's fixed ``owner/repo``, the
        project slug may span 3+ segments. Virtual package shorthand must not
        chop a nested group path after two segments.

        Shorthand cannot disambiguate every deep namespace; ambiguous cases use
        object form with ``git:`` + ``path:`` in ``apm.yml``.

        This does **not** split extension-less paths (e.g. ``.../registry/pkg``)
        into repo + virtual: that would mis-parse valid 5+ segment project
        paths; use ``parse_from_dict`` with an explicit ``path`` for those.
        """
        n = len(path_segments)
        if n < 2:
            return n

        if has_collection and "collections" in path_segments:
            coll_idx = path_segments.index("collections")
            if coll_idx >= 2:
                return coll_idx
            return n

        if has_virtual_ext:
            for idx, seg in enumerate(path_segments):
                if idx >= 2 and seg in cls._GITLAB_VIRTUAL_ROOT_SEGMENTS:
                    return idx
            if n == 3:
                return 2
            if n == 4:
                return 3
            if n >= 5:
                return 3
            return 2

        return n

    @classmethod
    def _bare_shorthand_repo_segment_count(cls, path_segments: list[str]) -> int:
        """Return how many leading segments belong to the repo path for bare shorthand.

        For ``owner/repo[/...]`` shorthand without an FQDN, the default is 2
        segments (GitHub convention).  When registry-only mode is active, the
        proxy may be fronting a host that allows nested namespaces (GitLab
        subgroups) -- parse defaults to **all-as-repo** so the deterministic
        boundary probe in :mod:`apm_cli.install.artifactory_resolver` can
        rebuild the dependency reference at the proxy-verified split.

        The only parse-time inference kept is **structural**: a path whose
        last segment ends in a virtual file extension
        (``.prompt.md``/``.instructions.md``/``.agent.md``)
        is by shape a virtual file dep -- the file is the last segment and
        the repo is everything before it.  This is not a directory-marker
        heuristic; the file extension is the type.  The shallower boundary
        (when the file lives under a known directory like ``prompts/``) is
        settled by the probe, not by a marker list.
        """
        n = len(path_segments)
        if n < 3:
            return 2

        from ...deps.registry_proxy import is_enforce_only

        if not is_enforce_only():
            return 2

        if any(path_segments[-1].endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS):
            return n - 1
        return n

    @classmethod
    def _parse_registry_object_entry(cls, entry: dict) -> "DependencyReference":
        """Parse the object-form registry entry per §3.2.

        Required keys:
            id:       <owner>/<repo>   # package identity at the registry
            version:  <any-string>      # opaque version string; registry resolves it

        Optional:
            registry: <name>           # routes to named registry; omit to use default
            path:     prompts/foo.md   # virtual sub-path; omit to install the whole package
            alias:    <name>           # same meaning as in other object forms
        """
        from ...deps.registry.feature_gate import require_package_registry_enabled

        require_package_registry_enabled("Object-form registry dependencies")

        _registry_raw = entry.get("registry")
        registry_name: str | None = None
        if _registry_raw is not None:
            if not isinstance(_registry_raw, str) or not _registry_raw.strip():
                raise ValueError(
                    "Object-form registry entry: 'registry' must be a non-empty "
                    "string (the name of an entry in the apm.yml registries: block)"
                )
            registry_name = _registry_raw.strip()

        pkg_id = entry.get("id")
        if not isinstance(pkg_id, str) or not pkg_id.strip():
            raise ValueError(
                "Object-form registry entry: 'id' is required and must be a "
                "non-empty 'owner/repo' string"
            )
        pkg_id = pkg_id.strip()
        if "/" not in pkg_id:
            raise ValueError(
                f"Object-form registry entry: 'id' must be 'owner/repo', got {pkg_id!r}"
            )

        raw_path = entry.get("path")
        sub_path: str | None = None
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError(
                    "Object-form registry entry: 'path' must be a non-empty string "
                    "when provided (e.g. 'prompts/review.prompt.md')"
                )
            sub_path = raw_path.strip().strip("/").replace("\\", "/").strip("/")
            validate_path_segments(sub_path, context="path")

        version = entry.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Object-form registry entry: 'version' is required")
        version = version.strip()

        alias = entry.get("alias")
        if alias is not None:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("'alias' field must be a non-empty string")
            alias = alias.strip()
            if not re.match(r"^[a-zA-Z0-9._-]+$", alias):
                raise ValueError(
                    f"Invalid alias: {alias}. Aliases can only contain "
                    f"letters, numbers, dots, underscores, and hyphens"
                )

        # Reject any unknown keys to catch typos early.
        known = {"registry", "id", "path", "version", "alias"}
        unknown = set(entry.keys()) - known
        if unknown:
            raise ValueError(
                f"Object-form registry entry has unknown fields: "
                f"{sorted(unknown)}. Known fields: {sorted(known)}"
            )

        owner_segments = pkg_id.split("/")
        validate_path_segments(pkg_id, context="registry id")
        for seg in owner_segments:
            if not re.match(r"^[a-zA-Z0-9._-]+$", seg):
                raise ValueError(f"Invalid registry id segment: {seg!r} in {pkg_id!r}")

        return cls(
            repo_url=pkg_id,
            host=default_host(),
            reference=version,
            virtual_path=sub_path,
            is_virtual=sub_path is not None,
            alias=alias,
            source="registry",
            registry_name=registry_name,
        )

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
                any(seg.endswith(ext) for ext in cls.VIRTUAL_FILE_EXTENSIONS)
                for seg in path_segments
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
            # Bare shorthand (no FQDN).  Default GitHub-style: owner/repo plus
            # any tail is treated as a virtual sub-path.  But when registry-only
            # mode is active, the proxy may be fronting a GitLab instance where
            # the project lives at an arbitrary subgroup depth -- fold non-marker
            # segments into the repo path instead of mis-classifying them as
            # virtual sub-paths (see issue: nested GitLab subgroup support).
            min_base_segments = cls._bare_shorthand_repo_segment_count(path_segments)

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

        url_virtual_path: str | None = None

        if is_ado_host:
            # *.visualstudio.com encodes org in the subdomain; URL path is proj/repo (2 parts).
            # dev.azure.com encodes org as the first path segment; URL path is org/proj/repo (3 parts).
            is_vs_legacy = is_visualstudio_legacy_hostname(hostname)
            min_ado_parts = 2 if is_vs_legacy else 3
            if len(path_parts) < min_ado_parts:
                raise ValueError(
                    f"Invalid Azure DevOps repository path: expected 'org/project/repo', got '{path}'"
                )
            if len(path_parts) > min_ado_parts:
                # Extra segments are a virtual sub-path (e.g. sub/path in
                # https://dev.azure.com/org/proj/_git/repo/sub/path or
                # https://myorg.visualstudio.com/proj/_git/repo/sub/path).
                ado_virtual = "/".join(path_parts[min_ado_parts:])

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

            # For *.visualstudio.com, inject the org from the subdomain so that the
            # normalised repo_url is always org/project/repo (matching dev.azure.com).
            if is_vs_legacy:
                vs_org = hostname.split(".")[0]
                path_parts = [vs_org, *path_parts]
        else:
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

        allowed_pattern = _path_segment_pattern(is_ado_host)
        validate_path_segments(
            "/".join(path_parts),
            context="repository URL path",
            reject_empty=True,
        )
        for part in path_parts:
            if not re.match(allowed_pattern, part):
                raise ValueError(f"Invalid repository path component: {part}")

        return "/".join(path_parts), url_virtual_path

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

    @classmethod
    def parse(cls, dependency_str: str) -> "DependencyReference":
        """Parse a dependency string into a DependencyReference.

        Supports formats:
        - user/repo
        - user/repo#branch
        - user/repo#v1.0.0
        - user/repo#commit_sha
        - github.com/user/repo#ref
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
                source="local",
            )

        if dependency_str.startswith("//"):
            raise ValueError(
                unsupported_host_error("//...", context="Protocol-relative URLs are not supported")
            )

        cls._reject_shorthand_alias(dependency_str)

        maybe_raise_bare_fqdn_github_gitlab_conflict(dependency_str)

        # Guard: detect a subpath embedded in an explicit git URL form (#872).
        # Fires before virtual-package detection so the user gets an actionable
        # error rather than a cryptic downstream git failure.
        cls._check_no_embedded_subpath(dependency_str)

        # Phase 1: detect virtual packages
        is_virtual_package, virtual_path, validated_host = cls._detect_virtual_package(
            dependency_str
        )

        # Phase 2: parse SSH (ssh:// URL first -- it preserves port; then SCP
        # shorthand), otherwise fall back to HTTPS/shorthand parsing.
        explicit_scheme: str | None = None
        ssh_user: str | None = None
        ssh_proto_result = cls._parse_ssh_protocol_url(dependency_str)
        if ssh_proto_result:
            host, port, repo_url, reference, alias, ssh_user = ssh_proto_result
            explicit_scheme = "ssh"
        else:
            scp_result = cls._parse_ssh_url(dependency_str)
            if scp_result:
                host, port, repo_url, reference, alias, ssh_user = scp_result
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
            ssh_user=ssh_user,
        )

    def to_apm_yml_entry(self):
        """Return the entry to store in apm.yml.

        - Local path deps with optional fields: returns a dict with 'path'.
        - HTTP (insecure) git deps: returns a dict with 'git' and 'allow_insecure' keys.
        - Git deps with skill_subset or target_subset: returns a dict with 'git' plus
          the applicable optional keys.
        - All other deps: returns the canonical string (same as to_canonical()).

        Returns:
            str or dict: String for simple deps; dict for local-with-subsets, HTTP, or
            skill/target-subset deps.

        Raises:
            ValueError: If this is an unresolved marketplace dependency.
        """
        if self.is_marketplace:
            raise ValueError(
                f"Cannot serialize unresolved marketplace dependency "
                f"'{self.marketplace_plugin_name}@{self.marketplace_name}'"
            )
        if self.is_local and self.local_path:
            if self.skill_subset or self.target_subset or self.alias:
                return local_path_apm_yml_entry(
                    self.local_path,
                    self.alias,
                    self.skill_subset,
                    self.target_subset,
                )
            return self.to_canonical()
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
            if self.target_subset:
                entry["targets"] = sorted(self.target_subset)
            return entry
        if self.skill_subset or self.target_subset:
            entry = {"git": self.get_identity()}
            if self.reference:
                entry["ref"] = self.reference
            if self.alias:
                entry["alias"] = self.alias
            if self.skill_subset:
                entry["skills"] = sorted(self.skill_subset)
            if self.target_subset:
                entry["targets"] = sorted(self.target_subset)
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
