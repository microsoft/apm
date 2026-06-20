"""Serialization builders for :class:`LockedDependency`.

Extracted to keep ``lockfile.py`` under the 800-line ceiling. The builders
take the owning class as their first argument, so this module imports nothing
from ``lockfile`` at load time (no circular import). ``lockfile`` re-imports
these names and the two small validators back into its own namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference
    from .lockfile import LockedDependency

_ALLOWED_HOST_TYPES = {"gitlab"}


def _normalize_lockfile_host_type(raw: Any) -> str | None:
    """Validate and normalize the optional lockfile host_type field."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("lockfile host_type must be a non-empty string")
    value = raw.strip().lower()
    if value not in _ALLOWED_HOST_TYPES:
        raise ValueError(
            f"Unsupported lockfile host_type: {raw}. Supported values: "
            f"{', '.join(sorted(_ALLOWED_HOST_TYPES))}"
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

    host_type = _normalize_lockfile_host_type(data.get("host_type"))

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
        # legacy migration key handled above
        "deployed_skills",
    }
    unknown_fields = {k: v for k, v in data.items() if k not in _known_keys}

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

    # When a git-semver resolution is present, prefer the concrete
    # resolved tag for ``resolved_ref`` (so subsequent installs see a
    # literal tag, not the original range). The original constraint
    # is preserved in the dedicated ``constraint`` field.
    if git_semver_resolution is not None:
        resolved_ref_val: str | None = git_semver_resolution.resolved_tag
    else:
        resolved_ref_val = dep_ref.reference

    return cls(
        repo_url=dep_ref.repo_url,
        host=host,
        host_type=dep_ref.host_type,
        port=dep_ref.port,
        registry_prefix=registry_prefix,
        resolved_commit=resolved_commit,
        resolved_ref=resolved_ref_val,
        version=(
            registry_resolution.version
            if registry_resolution is not None
            else (
                git_semver_resolution.resolved_version
                if git_semver_resolution is not None
                else None
            )
        ),
        virtual_path=dep_ref.virtual_path,
        is_virtual=dep_ref.is_virtual,
        depth=depth,
        resolved_by=resolved_by,
        source=source,
        local_path=dep_ref.local_path if dep_ref.is_local else None,
        is_dev=is_dev,
        is_insecure=dep_ref.is_insecure,
        allow_insecure=dep_ref.allow_insecure,
        skill_subset=sorted(dep_ref.skill_subset)
        if isinstance(getattr(dep_ref, "skill_subset", None), list)
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
    )
