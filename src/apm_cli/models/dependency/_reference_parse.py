"""Object-entry + top-level string parsing mixin for ``DependencyReference``.

Composed onto :class:`~apm_cli.models.dependency.reference.DependencyReference`
via mixin inheritance; ``cls`` binds to ``DependencyReference`` at call time and
cross-method calls (``cls.parse``, ``cls._detect_virtual_package``, ...) resolve
through the MRO. Nothing here imports the composed class, so the package stays
free of import cycles.
"""

import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import (
    default_host,
    is_azure_devops_hostname,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
    unsupported_host_error,
)
from ...utils.path_security import validate_path_segments

if TYPE_CHECKING:
    from .reference import DependencyReference

_MARKETPLACE_KEYS = {"name", "marketplace", "version"}


class _ReferenceParseMixin:
    """``parse`` / ``parse_from_dict`` and their object-entry sub-parsers."""

    @staticmethod
    def _parse_host_type(raw: object) -> str | None:
        """Parse the optional object-form ``type`` host-kind hint.

        Currently only ``gitlab`` is accepted; any other value fails closed with
        a ``ValueError``. This is a deliberate gate, not an oversight: future
        host kinds (e.g. ``gitea``, ``bitbucket``) would extend the accepted set
        here and thread a matching branch through ``AuthResolver.classify_host``
        and ``host_backends.backend_for``. Until those backends exist, rejecting
        unknown hints keeps classification explicit rather than silently
        mis-routing a bespoke host to the GitHub path.
        """
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("'type' field must be a non-empty string")
        value = raw.strip().lower()
        if value != "gitlab":
            raise ValueError(f"'type' field only supports 'gitlab'; got {raw!r}")
        return value

    @classmethod
    def _check_no_embedded_subpath(cls, url: str) -> None:
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
        primitive_dirs = getattr(cls, "_APM_PRIMITIVE_DIRS", frozenset())
        for idx, seg in enumerate(segments[:-1]):
            if idx >= 2 and seg in primitive_dirs:
                raise ValueError(
                    "A subpath cannot be embedded in a git URL. "
                    f"Got: `{raw}`. "
                    "Use the `path:` key instead: "
                    "`git: <repo-url>` + `path: <primitive>/<name>` "
                    "(or the shorthand `org/repo/<primitive>/<name>`). "
                    "See https://microsoft.github.io/apm/consumer/manage-dependencies/"
                )

    @staticmethod
    def _validate_object_alias(alias_override: object) -> str:
        """Strip and validate an object-form ``alias`` value.

        Shared by every object entry shape (git, parent, registry) so the
        alias grammar stays defined in exactly one place.
        """
        if not isinstance(alias_override, str) or not alias_override.strip():
            raise ValueError("'alias' field must be a non-empty string")
        alias_override = alias_override.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", alias_override):
            raise ValueError(
                f"Invalid alias: {alias_override}. Aliases can only contain letters, "
                f"numbers, dots, underscores, and hyphens"
            )
        return alias_override

    @staticmethod
    def _validate_object_ref(ref_override: object) -> str:
        """Strip and validate an object-form ``ref`` value."""
        if not isinstance(ref_override, str) or not ref_override.strip():
            raise ValueError("'ref' field must be a non-empty string")
        return ref_override.strip()

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
            return cls._parse_marketplace_object_entry(entry)

        # Object-form registry package -- design s3.2.
        # Discriminated by the ``registry:`` or ``id:`` key (``registry:`` is
        # optional when a ``registries.default:`` is configured).  Mutually
        # exclusive with ``git:``.
        if "registry" in entry or "id" in entry:
            if "git" in entry:
                raise ValueError(
                    "Object-style dependency cannot mix 'registry:'/'id:' and 'git:' "
                    "keys -- choose one resolver."
                )
            return cls._parse_registry_object_entry(entry)

        # Support dict-form local path: { path: ./local/dir }
        if "path" in entry and "git" not in entry:
            return cls._parse_local_path_object_entry(entry)

        if "git" not in entry:
            raise ValueError(
                "Object-style dependency must have a 'git', 'path', or 'registry' field"
            )

        git_url = entry["git"]
        if not isinstance(git_url, str) or not git_url.strip():
            raise ValueError("'git' field must be a non-empty string")

        # Monorepo parent inheritance (literal ``git: parent`` only; resolver expands)
        if git_url == "parent":
            return cls._parse_parent_inheritance_entry(entry)

        return cls._parse_git_object_entry(entry, git_url)

    @classmethod
    def _parse_marketplace_object_entry(cls, entry: dict) -> "DependencyReference":
        """Parse a ``{ name, marketplace, version }`` marketplace entry."""
        source_keys = {"git", "path", "registry", "id"}.intersection(entry)
        if source_keys:
            joined = "', '".join(sorted(source_keys))
            raise ValueError(
                f"Ambiguous dependency: 'marketplace' cannot be combined with '{joined}'"
            )
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

    @classmethod
    def _parse_local_path_object_entry(cls, entry: dict) -> "DependencyReference":
        """Parse a ``{ path: ./local/dir }`` dict-form local path entry."""
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

    @classmethod
    def _parse_parent_inheritance_entry(cls, entry: dict) -> "DependencyReference":
        """Parse a ``git: parent`` monorepo-inheritance object entry."""
        if entry.get("type") is not None:
            raise ValueError("'type' is only supported for remote git dependencies")
        path_raw = entry.get("path")
        if path_raw is None:
            raise ValueError("Object-style dependency with git: 'parent' requires a 'path' field")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise ValueError("'path' field must be a non-empty string")
        normalized_path = cls._normalize_parent_repo_decl_path(path_raw)

        ref_override = entry.get("ref")
        reference: str | None = None
        if ref_override is not None:
            reference = cls._validate_object_ref(ref_override)

        alias_override = entry.get("alias")
        alias_val: str | None = None
        if alias_override is not None:
            alias_val = cls._validate_object_alias(alias_override)

        return cls(
            repo_url="_parent",
            host=None,
            reference=reference,
            alias=alias_val,
            virtual_path=normalized_path,
            is_virtual=True,
            is_parent_repo_inheritance=True,
        )

    @classmethod
    def _parse_git_object_entry(cls, entry: dict, git_url: str) -> "DependencyReference":
        """Parse a standard ``git:`` object entry and apply its overrides."""
        sub_path = entry.get("path")
        allow_insecure = entry.get("allow_insecure", False)
        if not isinstance(allow_insecure, bool):
            raise ValueError("'allow_insecure' field must be a boolean")

        host_type = cls._parse_host_type(entry.get("type"))

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
        ref_override = entry.get("ref")
        if ref_override is not None:
            dep.reference = cls._validate_object_ref(ref_override)

        alias_override = entry.get("alias")
        if alias_override is not None:
            dep.alias = cls._validate_object_alias(alias_override)

        # Apply sub-path as virtual package
        if sub_path:
            dep.virtual_path = sub_path
            dep.is_virtual = True

        # Parse skills: field (SKILL_BUNDLE subset selection)
        skills_raw = entry.get("skills")
        if skills_raw is not None:
            dep.skill_subset = cls._parse_skill_subset(skills_raw)

        return dep

    @staticmethod
    def _parse_skill_subset(skills_raw: object) -> list[str]:
        """Validate and de-duplicate the ``skills:`` subset list."""
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
        return sorted(validated)

    @classmethod
    def _parse_registry_object_entry(cls, entry: dict) -> "DependencyReference":
        """Parse the object-form registry entry per s3.2.

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
            alias = cls._validate_object_alias(alias)

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
