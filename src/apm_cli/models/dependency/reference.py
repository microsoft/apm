"""DependencyReference model  -- core dependency representation and parsing.

The class body is split across cohesive mixins to keep each module small and
focused while preserving a single patchable ``DependencyReference`` symbol:

* :class:`_ReferenceParseMixin` -- ``parse`` / ``parse_from_dict`` + object entries
* :class:`_ReferenceUrlMixin`   -- HTTPS / SSH / shorthand URL resolution
* :class:`_ReferenceShorthandMixin` -- virtual-package + boundary heuristics

Mixin classmethods inherit onto the composed class, so ``cls`` binds to
``DependencyReference`` and inter-method calls resolve via the MRO. Leaf
helpers and constants live in :mod:`._reference_util` (re-exported below) so
the mixin modules never import this module back -- no circular import.
"""

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import (
    default_host,
    validate_ssh_user,
)
from ...utils.path_security import (
    ensure_path_within,
    validate_path_segments,
)
from ._reference_parse import _ReferenceParseMixin
from ._reference_shorthand import _ReferenceShorthandMixin
from ._reference_url import _ReferenceUrlMixin

# Re-export relocated leaf helpers/constants so that
# ``apm_cli.models.dependency.reference.NAME`` keeps resolving for any code
# (and tests) importing them from here. The helpers themselves live in
# ``_reference_util`` to break the mixin <-> reference import cycle.
from ._reference_util import (
    _ADO_PATH_SEGMENT_RE as _ADO_PATH_SEGMENT_RE,
)
from ._reference_util import (
    _DEFAULT_SCHEME_PORTS as _DEFAULT_SCHEME_PORTS,
)
from ._reference_util import (
    _NON_ADO_PATH_SEGMENT_RE as _NON_ADO_PATH_SEGMENT_RE,
)
from ._reference_util import (
    _RANGE_PREFIX_RE as _RANGE_PREFIX_RE,
)
from ._reference_util import (
    _REF_VERSION_SUFFIX_RE as _REF_VERSION_SUFFIX_RE,
)
from ._reference_util import (
    InvalidSemverRangeError as InvalidSemverRangeError,
)
from ._reference_util import (
    _is_valid_registry_semver_range as _is_valid_registry_semver_range,
)
from ._reference_util import (
    _looks_like_invalid_semver_range as _looks_like_invalid_semver_range,
)
from ._reference_util import (
    _path_segment_pattern as _path_segment_pattern,
)
from .identity import (
    build_canonical_dependency_string as build_canonical_dependency_string,
)
from .identity import (
    build_dependency_unique_key as build_dependency_unique_key,
)
from .types import VirtualPackageType


@dataclass
class DependencyReference(_ReferenceParseMixin, _ReferenceUrlMixin, _ReferenceShorthandMixin):
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

    # Monorepo inheritance: { git: parent, path: ... } -- expanded in resolver
    is_parent_repo_inheritance: bool = False

    artifactory_prefix: str | None = None  # e.g., "artifactory/github" (repo key path)

    # HTTP (insecure) dependency fields
    is_insecure: bool = False  # True when the dependency URL uses http://
    allow_insecure: bool = False  # True if this HTTP dep is explicitly allowed

    # SKILL_BUNDLE subset selection (persisted in apm.yml `skills:` field)
    skill_subset: list[str] | None = None  # Sorted skill names, or None = all

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
    # lockfile (the lockfile uses URL-based identity per design s6.1).
    source: str | None = None
    registry_name: str | None = None

    # Marketplace dependency fields (parsed from plugin.json dict format)
    is_marketplace: bool = False
    marketplace_name: str | None = None
    marketplace_plugin_name: str | None = None
    marketplace_version_spec: str | None = None

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
        ".chatmode.md",
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
            "chatmodes",
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
        ``.prompt.md``/``.instructions.md``/``.chatmode.md``/``.agent.md``
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
        )

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

    def to_apm_yml_entry(self):
        """Return the entry to store in apm.yml.

        For HTTP (insecure) deps, returns a dict with 'git' and 'allow_insecure' keys.
        For deps with skill_subset, returns a dict with 'git' and 'skills' keys.
        For all other deps, returns the canonical string (same as to_canonical()).

        Returns:
            str or dict: String for simple deps; dict for HTTP or skill-subset deps.

        Raises:
            ValueError: If this is an unresolved marketplace dependency.
        """
        if self.is_marketplace:
            raise ValueError(
                f"Cannot serialize unresolved marketplace dependency "
                f"'{self.marketplace_plugin_name}@{self.marketplace_name}'"
            )
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
