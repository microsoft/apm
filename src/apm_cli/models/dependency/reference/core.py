"""DependencyReference model  -- core dependency representation and parsing."""

from dataclasses import dataclass

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


@dataclass
class DependencyReference:
    """Represents a reference to an APM dependency."""

    repo_url: str  # e.g., "user/repo" for GitHub or "org/project/repo" for Azure DevOps
    host: str | None = None  # Optional host (github.com, dev.azure.com, or enterprise host)
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

    # Monorepo inheritance: { git: parent, path: ... } — expanded in resolver
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


from .identity import (
    __str__,
    canonicalize,
    get_canonical_dependency_string,
    get_display_name,
    get_identity,
    get_unique_key,
    to_canonical,
)
from .parsing import (
    _detect_explicit_scheme,
    _handle_local_path,
    _normalize_parent_repo_decl_path,
    _parse_object_git_overrides,
    _parse_object_local_path,
    _parse_object_parent,
    _parse_ssh_forms,
    _parse_ssh_protocol_url,
    _parse_ssh_url,
    _parse_standard_url,
    parse,
    parse_from_dict,
)
from .paths import (
    _get_local_install_path,
    _get_regular_install_path,
    _get_virtual_file_install_path,
    _get_virtual_subdirectory_install_path,
    get_install_path,
    is_artifactory,
    is_azure_devops,
    is_local_path,
)
from .serialization import to_apm_yml_entry, to_clone_url, to_github_url
from .shorthand_gitlab import (
    _gitlab_shorthand_repo_segment_count,
    from_gitlab_shorthand_probe,
    iter_gitlab_direct_shorthand_boundary_candidates,
    needs_gitlab_direct_shorthand_probing,
    split_gitlab_direct_shorthand_parts,
)
from .shorthand_resolve import (
    _extract_user_repo_from_parts,
    _extract_user_repo_shorthand,
    _resolve_ado_virtual_shorthand,
    _resolve_explicit_host_virtual,
    _resolve_gitlab_virtual_shorthand,
    _resolve_implicit_host_virtual,
    _resolve_shorthand_to_parsed_url,
    _resolve_virtual_shorthand_repo,
    _validate_user_repo_format,
)
from .validation import (
    _extract_artifactory_prefix,
    _validate_ado_path,
    _validate_final_repo_fields,
    _validate_non_ado_path,
    _validate_path_components,
    _validate_url_repo_path,
)
from .virtual import (
    get_virtual_package_name,
    is_virtual_file,
    is_virtual_subdirectory,
    virtual_type,
)
from .virtual_detect import (
    _compute_min_base_segments,
    _detect_virtual_package,
    virtual_suffix_is_installable_shape,
)

DependencyReference.virtual_type = virtual_type
DependencyReference.is_virtual_file = is_virtual_file
DependencyReference.is_virtual_subdirectory = is_virtual_subdirectory
DependencyReference.get_virtual_package_name = get_virtual_package_name
DependencyReference.get_unique_key = get_unique_key
DependencyReference.to_canonical = to_canonical
DependencyReference.get_identity = get_identity
DependencyReference.canonicalize = canonicalize
DependencyReference.get_canonical_dependency_string = get_canonical_dependency_string
DependencyReference.get_display_name = get_display_name
DependencyReference.__str__ = __str__
DependencyReference.is_artifactory = is_artifactory
DependencyReference.is_azure_devops = is_azure_devops
DependencyReference.is_local_path = is_local_path
DependencyReference.get_install_path = get_install_path
DependencyReference._get_local_install_path = _get_local_install_path
DependencyReference._get_regular_install_path = _get_regular_install_path
DependencyReference._get_virtual_file_install_path = _get_virtual_file_install_path
DependencyReference._get_virtual_subdirectory_install_path = _get_virtual_subdirectory_install_path
DependencyReference._parse_ssh_protocol_url = _parse_ssh_protocol_url
DependencyReference._normalize_parent_repo_decl_path = _normalize_parent_repo_decl_path
DependencyReference._parse_object_local_path = _parse_object_local_path
DependencyReference._parse_object_parent = _parse_object_parent
DependencyReference._parse_object_git_overrides = _parse_object_git_overrides
DependencyReference.parse_from_dict = parse_from_dict
DependencyReference._parse_ssh_url = _parse_ssh_url
DependencyReference._parse_ssh_forms = _parse_ssh_forms
DependencyReference._detect_explicit_scheme = _detect_explicit_scheme
DependencyReference._parse_standard_url = _parse_standard_url
DependencyReference._handle_local_path = _handle_local_path
DependencyReference.parse = parse
DependencyReference.split_gitlab_direct_shorthand_parts = split_gitlab_direct_shorthand_parts
DependencyReference.needs_gitlab_direct_shorthand_probing = needs_gitlab_direct_shorthand_probing
DependencyReference.iter_gitlab_direct_shorthand_boundary_candidates = (
    iter_gitlab_direct_shorthand_boundary_candidates
)
DependencyReference.from_gitlab_shorthand_probe = from_gitlab_shorthand_probe
DependencyReference._gitlab_shorthand_repo_segment_count = _gitlab_shorthand_repo_segment_count
DependencyReference.virtual_suffix_is_installable_shape = virtual_suffix_is_installable_shape
DependencyReference._compute_min_base_segments = _compute_min_base_segments
DependencyReference._detect_virtual_package = _detect_virtual_package
DependencyReference._resolve_virtual_shorthand_repo = _resolve_virtual_shorthand_repo
DependencyReference._resolve_ado_virtual_shorthand = _resolve_ado_virtual_shorthand
DependencyReference._resolve_gitlab_virtual_shorthand = _resolve_gitlab_virtual_shorthand
DependencyReference._resolve_explicit_host_virtual = _resolve_explicit_host_virtual
DependencyReference._resolve_implicit_host_virtual = _resolve_implicit_host_virtual
DependencyReference._extract_user_repo_from_parts = _extract_user_repo_from_parts
DependencyReference._extract_user_repo_shorthand = _extract_user_repo_shorthand
DependencyReference._validate_user_repo_format = _validate_user_repo_format
DependencyReference._resolve_shorthand_to_parsed_url = _resolve_shorthand_to_parsed_url
DependencyReference._validate_url_repo_path = _validate_url_repo_path
DependencyReference._validate_ado_path = _validate_ado_path
DependencyReference._validate_non_ado_path = _validate_non_ado_path
DependencyReference._validate_path_components = _validate_path_components
DependencyReference._validate_final_repo_fields = _validate_final_repo_fields
DependencyReference._extract_artifactory_prefix = _extract_artifactory_prefix
DependencyReference.to_apm_yml_entry = to_apm_yml_entry
DependencyReference.to_github_url = to_github_url
DependencyReference.to_clone_url = to_clone_url
