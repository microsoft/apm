"""Serialization builders for :class:`LockedDependency`.

Extracted to keep ``lockfile.py`` under the 800-line ceiling. The builders
take the owning class as their first argument, so this module imports nothing
from ``lockfile`` at load time (no circular import). ``lockfile`` re-imports
these names and the two small validators back into its own namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models.apm_package import DependencyReference

if TYPE_CHECKING:
    from .lockfile import LockedDependency

_ALLOWED_HOST_TYPES: set[str] | None = None

_ALLOWED_EXEC_STATUS = {"deployed", "gated_pending_approval", "denied", "absent"}


class LockfileFormatError(ValueError):
    """Raised when a lockfile container does not match its schema."""


class UnsupportedLockfileVersionError(LockfileFormatError):
    """Raised when a lockfile declares a version this client cannot read."""


def _get_allowed_host_types() -> set[str]:
    global _ALLOWED_HOST_TYPES
    if _ALLOWED_HOST_TYPES is None:
        try:
            from ..core.host_providers import accepted_host_types

            _ALLOWED_HOST_TYPES = set(accepted_host_types())
        except Exception:
            _ALLOWED_HOST_TYPES = {"gitlab"}
    return _ALLOWED_HOST_TYPES


def _validate_lockfile_container(data: object) -> dict[str, Any]:
    """Validate version and top-level container shapes before construction."""
    # Defer to lockfile.py's accessor -- the constant is owned there (#1078).
    from .lockfile import _get_supported_versions

    _supported_versions = _get_supported_versions()
    if not isinstance(data, dict):
        raise LockfileFormatError("Lockfile root must be a mapping")
    data = dict(data)
    version = data.get("lockfile_version", "1")
    if not isinstance(version, str) or version not in _supported_versions:
        supported = ", ".join(sorted(_supported_versions))
        raise UnsupportedLockfileVersionError(
            f"Unsupported lockfile version {version!r}; supported versions: {supported}"
        )
    list_fields = (
        "dependencies",
        "deployments",
        "mcp_servers",
        "lsp_servers",
        "local_deployed_files",
    )
    mapping_fields = (
        "mcp_configs",
        "mcp_target_servers",
        "mcp_config_provenance",
        "lsp_configs",
        "local_deployed_file_hashes",
    )
    for field_name in list_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = []
        elif field_name in data and not isinstance(data[field_name], list):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a list")
    for field_name in mapping_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = {}
        elif field_name in data and not isinstance(data[field_name], dict):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a mapping")
    for index, dependency in enumerate(data.get("dependencies", [])):
        if not isinstance(dependency, dict):
            raise LockfileFormatError(f"Lockfile dependency at index {index} must be a mapping")
    for target, servers in (data.get("mcp_target_servers") or {}).items():
        if not isinstance(target, str) or not target or not isinstance(servers, list):
            raise LockfileFormatError(
                "Lockfile mcp_target_servers values must be string-to-list mappings"
            )
        if not all(isinstance(server, str) and bool(server) for server in servers):
            raise LockfileFormatError("Lockfile mcp_target_servers entries must be strings")
    for server, provenance in (data.get("mcp_config_provenance") or {}).items():
        if not isinstance(server, str) or not (
            (isinstance(provenance, str) and bool(provenance))
            or (
                isinstance(provenance, list)
                and bool(provenance)
                and all(isinstance(owner, str) and bool(owner) for owner in provenance)
            )
        ):
            raise LockfileFormatError(
                "Lockfile mcp_config_provenance values must be strings or string lists"
            )
    if "deployments" in data:
        from ..core.deployment_ledger import DeploymentLedgerCodec

        try:
            DeploymentLedgerCodec.validate_rows(data["deployments"])
        except ValueError as exc:
            raise LockfileFormatError(str(exc)) from exc
    return data


def _normalized_mcp_provenance(
    provenance: dict[str, str | list[str]],
) -> dict[str, str | list[str]]:
    """Return deterministic MCP provenance with list-valued owners sorted."""
    return {
        server: sorted(owners) if isinstance(owners, list) else owners
        for server, owners in sorted(provenance.items())
    }


def _normalize_exec_status(raw: Any) -> str | None:
    """Validate and normalize the optional executable-trust status."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("lockfile exec_status must be a non-empty string")
    value = raw.strip()
    if value not in _ALLOWED_EXEC_STATUS:
        raise ValueError(
            f"Unsupported lockfile exec_status: {raw}. Supported values: "
            f"{', '.join(sorted(_ALLOWED_EXEC_STATUS))}"
        )
    return value


def _normalize_lockfile_host_type(raw: Any) -> str | None:
    """Validate and normalize the optional lockfile host_type field."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("lockfile host_type must be a non-empty string")
    value = raw.strip().lower()
    allowed = _get_allowed_host_types()
    if value not in allowed:
        raise ValueError(
            f"Unsupported lockfile host_type: {raw}. Supported values: {', '.join(sorted(allowed))}"
        )
    return value


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Return values without duplicates, preserving first-seen order."""
    return list(dict.fromkeys(values))


def locked_dependency_from_dict(
    cls: type[LockedDependency], data: dict[str, Any]
) -> LockedDependency:
    """Deserialize a :class:`LockedDependency` from a dict.

    Handles backwards compatibility:
    - Old ``deployed_skills`` lists are migrated to ``deployed_files``
      paths under ``.github/skills/`` and ``.claude/skills/``.
    """
    deployed_files = _dedupe_preserving_order(list(data.get("deployed_files", [])))

    # Migrate legacy deployed_skills -> deployed_files
    old_skills = data.get("deployed_skills", [])
    if old_skills and not deployed_files:
        for skill_name in old_skills:
            deployed_files.append(f".github/skills/{skill_name}/")
            deployed_files.append(f".claude/skills/{skill_name}/")

    # Defensive cast: reject non-numeric or out-of-range ports from tampered lockfiles.
    _p_raw = data.get("port")
    port: int | None = None
    if _p_raw is not None:
        try:
            _p_int = int(_p_raw)
        except (TypeError, ValueError):
            _p_int = None
        if _p_int is not None and 1 <= _p_int <= 65535:
            port = _p_int

    import sys

    _lockfile_mod = sys.modules.get("apm_cli.deps.lockfile")
    _normalize_ht = (
        getattr(_lockfile_mod, "_normalize_lockfile_host_type", None)
        or _normalize_lockfile_host_type
    )
    host_type = _normalize_ht(data.get("host_type"))

    # Recognised keys this build knows about. Anything else is captured
    # as ``_unknown_fields`` so a re-emit preserves forward-introduced
    # fields rather than silently dropping them. ``deployed_skills`` is
    # the explicit legacy key handled above; do NOT consider it unknown.
    _known_keys = {
        "repo_url",
        "host",
        "host_type",
        "port",
        "registry_prefix",
        "resolved_commit",
        "resolved_ref",
        "version",
        "virtual_path",
        "is_virtual",
        "depth",
        "resolved_by",
        "package_type",
        "deployed_files",
        "deployed_file_hashes",
        "source",
        "local_path",
        "content_hash",
        "is_dev",
        "discovered_via",
        "marketplace_plugin_name",
        "source_url",
        "source_digest",
        "is_insecure",
        "allow_insecure",
        "skill_subset",
        "resolved_url",
        "resolved_hash",
        "constraint",
        "resolved_tag",
        "resolved_at",
        "declared_license",
        "declaring_parent",
        "anchored_local_path",
        "target_subset",
        "exec_status",
        "name",
        # legacy migration key handled above
        "deployed_skills",
    }
    unknown_fields = {
        k: v
        for k, v in data.items()
        if k not in _known_keys and not DependencyReference.is_transient_provider_field(k)
    }

    import sys

    _lockfile_mod2 = sys.modules.get("apm_cli.deps.lockfile")
    _normalize_es = (
        getattr(_lockfile_mod2, "_normalize_exec_status", None) or _normalize_exec_status
    )
    exec_status = _normalize_es(data.get("exec_status"))

    return cls(
        repo_url=data["repo_url"],
        host=data.get("host"),
        host_type=host_type,
        port=port,
        registry_prefix=data.get("registry_prefix"),
        resolved_commit=data.get("resolved_commit"),
        resolved_ref=data.get("resolved_ref"),
        version=data.get("version"),
        virtual_path=data.get("virtual_path"),
        is_virtual=data.get("is_virtual", False),
        depth=data.get("depth", 1),
        resolved_by=data.get("resolved_by"),
        package_type=data.get("package_type"),
        deployed_files=deployed_files,
        deployed_file_hashes=dict(data.get("deployed_file_hashes") or {}),
        source=data.get("source"),
        local_path=data.get("local_path"),
        content_hash=data.get("content_hash"),
        is_dev=data.get("is_dev", False),
        discovered_via=data.get("discovered_via"),
        marketplace_plugin_name=data.get("marketplace_plugin_name"),
        source_url=data.get("source_url"),
        source_digest=data.get("source_digest"),
        is_insecure=data.get("is_insecure", False),
        allow_insecure=data.get("allow_insecure", False),
        skill_subset=list(data.get("skill_subset") or []),
        resolved_url=data.get("resolved_url"),
        resolved_hash=data.get("resolved_hash"),
        constraint=data.get("constraint"),
        resolved_tag=data.get("resolved_tag"),
        resolved_at=data.get("resolved_at"),
        declared_license=data.get("declared_license"),
        declaring_parent=data.get("declaring_parent"),
        anchored_local_path=data.get("anchored_local_path"),
        target_subset=list(data.get("target_subset") or []),
        exec_status=exec_status,
        name=data.get("name"),
        _unknown_fields=unknown_fields,
    )


def locked_dependency_from_ref(
    cls: type[LockedDependency],
    dep_ref: DependencyReference,
    resolved_commit: str | None,
    depth: int,
    resolved_by: str | None,
    is_dev: bool = False,
    registry_config=None,
    registry_resolution=None,
    git_semver_resolution=None,
    package_name: str | None = None,
    package_version: str | None = None,
) -> LockedDependency:
    """Create a :class:`LockedDependency` from a DependencyReference.

    Args:
        cls: The owning ``LockedDependency`` class (passed by the delegating
            classmethod so this module avoids importing it at load time).
        dep_ref: The resolved dependency reference.
        resolved_commit: Exact commit SHA that was installed, or ``None``.
        depth: Dependency tree depth.
        resolved_by: Parent repo URL, or ``None`` for direct dependencies.
        is_dev: Whether this is a dev-only dependency.
        registry_config: Optional :class:`~apm_cli.deps.registry_proxy.RegistryConfig`
            used for this download (Artifactory VCS proxy -- pre-existing
            concept, distinct from the new dedicated-registry resolver).
            When provided, ``host`` is set to the pure FQDN and
            ``registry_prefix`` to the URL path prefix.
        registry_resolution: Optional :class:`~apm_cli.deps.registry.resolver.RegistryResolution`
            produced by the dedicated-registry resolver. When provided,
            ``source`` is set to ``"registry"`` and ``resolved_url`` /
            ``resolved_hash`` / ``version`` are populated from it (the
            trust anchor for re-installs per design section 6.1).
        git_semver_resolution: Optional
            :class:`~apm_cli.deps.git_semver_resolver.GitSemverResolution`
            produced when a git-source dep had a semver range as ``ref:``.
            When provided, ``constraint`` / ``resolved_tag`` /
            ``resolved_at`` are populated and ``resolved_ref`` is set
            to the concrete tag (issue #1488). Mutually exclusive with
            ``registry_resolution``.

    Raises:
        ValueError: When both ``registry_resolution`` and
            ``git_semver_resolution`` are provided. The two resolution
            paths are mutually exclusive: a dependency is either
            registry-sourced (carries ``resolved_url`` / ``resolved_hash``)
            or git-source with a semver range (carries ``constraint`` /
            ``resolved_tag`` / ``resolved_at``). Combining both would
            produce an inconsistent lockfile entry (e.g. ``source=registry``
            while ``resolved_ref`` is overridden to a git tag).
    """
    if registry_resolution is not None and git_semver_resolution is not None:
        raise ValueError(
            "registry_resolution and git_semver_resolution are mutually "
            "exclusive: a dependency is either registry-sourced or a "
            "git-source semver resolution, not both."
        )
    if registry_config is not None:
        host = registry_config.host
        registry_prefix = registry_config.prefix
    else:
        host = dep_ref.host
        registry_prefix = None

    # Determine source: explicit registry resolution wins; else local;
    # else inherit from dep_ref.source (which may be "git" or None).
    if registry_resolution is not None:
        source = "registry"
    elif dep_ref.is_local:
        source = "local"
    else:
        source = None

    # Prefer the concrete resolved identifier for ``resolved_ref`` so that
    # ``build_update_plan`` can detect real version changes by comparing
    # old_ref (locked concrete) vs new_ref (freshly resolved concrete).
    # Registry deps: store the resolved version (e.g. "1.0.3"), not the
    # range ("^1.0.0").  Git-semver deps: store the resolved tag.  Both
    # preserve the original selector in their dedicated fields
    # (``version`` / ``constraint`` respectively).
    if git_semver_resolution is not None:
        resolved_ref_val: str | None = git_semver_resolution.resolved_tag
    elif registry_resolution is not None:
        resolved_ref_val = registry_resolution.version
    else:
        resolved_ref_val = dep_ref.reference

    dep_ref.validate_provider_coordinates()
    if registry_resolution is not None:
        version_value = registry_resolution.version
    elif git_semver_resolution is not None:
        version_value = git_semver_resolution.resolved_version
    elif source != "registry":
        version_value = package_version
    else:
        version_value = None

    return cls(
        repo_url=dep_ref.repo_url,
        host=host,
        host_type=dep_ref.host_type,
        port=dep_ref.port,
        registry_prefix=registry_prefix,
        resolved_commit=resolved_commit,
        resolved_ref=resolved_ref_val,
        version=version_value,
        virtual_path=dep_ref.virtual_path,
        is_virtual=dep_ref.is_virtual,
        depth=depth,
        resolved_by=resolved_by,
        source=source,
        local_path=dep_ref.local_path if dep_ref.is_local else None,
        declaring_parent=dep_ref.declaring_parent if dep_ref.is_local else None,
        anchored_local_path=dep_ref.anchored_local_path if dep_ref.is_local else None,
        is_dev=is_dev,
        is_insecure=dep_ref.is_insecure,
        allow_insecure=dep_ref.allow_insecure,
        skill_subset=sorted(dep_ref.skill_subset)
        if isinstance(getattr(dep_ref, "skill_subset", None), list)
        else [],
        target_subset=sorted(dep_ref.target_subset)
        if isinstance(getattr(dep_ref, "target_subset", None), list)
        else [],
        resolved_url=(
            registry_resolution.resolved_url if registry_resolution is not None else None
        ),
        resolved_hash=(
            registry_resolution.resolved_hash if registry_resolution is not None else None
        ),
        constraint=(
            git_semver_resolution.constraint if git_semver_resolution is not None else None
        ),
        resolved_tag=(
            git_semver_resolution.resolved_tag if git_semver_resolution is not None else None
        ),
        resolved_at=(
            git_semver_resolution.resolved_at if git_semver_resolution is not None else None
        ),
        name=package_name,
    )
