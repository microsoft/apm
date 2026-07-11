"""Lock file support for APM dependency resolution.

Provides deterministic, reproducible installs by capturing exact resolved versions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..models.apm_package import DependencyReference
from ..models.dependency.identity import normalize_package_repo_url
from ..models.dependency.reference import (
    build_canonical_dependency_string,
    build_dependency_unique_key,
)

logger = logging.getLogger(__name__)

_SELF_KEY = "."
_ALLOWED_HOST_TYPES = {"gitlab"}
_ALLOWED_EXEC_STATUS = {"deployed", "gated_pending_approval", "denied", "absent"}


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


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Return values without duplicates, preserving first-seen order."""
    return list(dict.fromkeys(values))


@dataclass
class LockedDependency:
    """A resolved dependency with exact commit/version information."""

    repo_url: str
    host: str | None = None
    host_type: str | None = None
    port: int | None = None  # Non-standard SSH/HTTPS port (e.g. 7999 for Bitbucket DC)
    registry_prefix: str | None = None  # Registry path prefix, e.g. "artifactory/github"
    resolved_commit: str | None = None
    resolved_ref: str | None = None
    version: str | None = None
    virtual_path: str | None = None
    is_virtual: bool = False
    depth: int = 1
    resolved_by: str | None = None
    package_type: str | None = None
    deployed_files: list[str] = field(default_factory=list)
    deployed_file_hashes: dict[str, str] = field(default_factory=dict)
    source: str | None = None  # "local" for local deps, None/absent for remote
    local_path: str | None = None  # Original local path. Direct deps: relative to
    # the project root (``./packages/foo``). Transitive deps: relative to the
    # package that declared them (``../sibling``), anchored via ``resolved_by``
    # (issue #857; see apm_cli.deps.path_anchoring).
    content_hash: str | None = None  # SHA-256 of package file tree
    is_dev: bool = False  # True for devDependencies
    discovered_via: str | None = None  # Marketplace name (provenance)
    marketplace_plugin_name: str | None = None  # Plugin name in marketplace
    source_url: str | None = None  # Canonical marketplace source URL
    source_digest: str | None = None  # sha256 digest of the marketplace manifest
    is_insecure: bool = False  # True when the locked source was http://
    allow_insecure: bool = False  # True when the manifest explicitly allowed HTTP
    skill_subset: list[str] = field(default_factory=list)  # Sorted skill names for SKILL_BUNDLE
    target_subset: list[str] = field(default_factory=list)  # Audit-only consumer target subset

    # Registry resolver fields (design §6.1).
    # Populated when source == "registry"; absent otherwise. resolved_hash is
    # the sole non-negotiable trust anchor on every install — bytes are fetched
    # from resolved_url and re-verified against this digest.
    resolved_url: str | None = None
    resolved_hash: str | None = None

    # Git-source semver resolution fields (issue #1488).
    # Populated when a git-source dependency carried a semver range
    # (e.g. ``^1.2.0``) that the install pipeline resolved against the
    # remote's tags. Lockfile version stays at "2" -- these fields are
    # purely additive and forward-compatible with old readers (which
    # ignore unknown keys via the explicit ``from_dict`` allowlist).
    constraint: str | None = None
    resolved_tag: str | None = None
    resolved_at: str | None = None

    # Declared-license provenance (issue #1777, U6). The SPDX expression the
    # dependency's manifest DECLARED at resolved_commit (apm.yml ``license:``
    # or plugin.json ``license``). This is a passthrough of an author claim --
    # APM never reads the LICENSE file text or concludes a license. Absence
    # means "not declared" (unknown); it is OMITTED from the serialized entry
    # rather than stored as a sentinel, so absence stays distinguishable from
    # an explicit declaration.
    declared_license: str | None = None
    # Resolved executable-trust state (issue #1873). One of ``deployed`` |
    # ``gated_pending_approval`` | ``denied`` | ``absent``, mirroring the
    # resolver ``trust_state``. Absence (``None``) means the package declared
    # no executable primitive; it is OMITTED from the serialized entry so a
    # never-gated package stays distinguishable from an explicitly-cleared one.
    exec_status: str | None = None
    # Package-declared name from the dependency's own apm.yml (issue #1888).
    # SELF-ASSERTED display/inventory metadata only -- NOT an identity anchor.
    # See to_dict/from_dict and the supply-chain boundary note in the lockfile
    # spec. Omitted from the serialized entry when absent.
    name: str | None = None
    # Forward-compat carrier: keys we don't recognise are preserved
    # through a from_dict / to_dict round-trip so an older APM build
    # reading a lockfile written by a newer build doesn't silently drop
    # fields when it re-emits.
    _unknown_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize case-insensitive package identity when loading or creating locks."""
        self.repo_url = normalize_package_repo_url(
            self.repo_url,
            host=self.host,
            source=self.source,
            registry_prefix=self.registry_prefix,
        )

    def get_unique_key(self) -> str:
        """Returns unique key for this dependency."""
        return build_dependency_unique_key(
            self.repo_url,
            host=self.host,
            source=self.source,
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
            registry_prefix=self.registry_prefix,
        )

    def get_canonical_dependency_string(self) -> str:
        """Host-blind canonical key for filesystem / orphan-detection matching.

        Mirrors :meth:`DependencyReference.get_canonical_dependency_string`:
        returns the bare ``repo_url`` (+ ``virtual_path``), never host-qualified,
        so it matches the host-blind ``apm_modules/`` layout. Use
        :meth:`get_unique_key` for the host-qualified lockfile dedup key.
        """
        return build_canonical_dependency_string(
            self.repo_url,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for YAML output."""
        result: dict[str, Any] = {"repo_url": self.repo_url}
        if self.name is not None:
            result["name"] = self.name
        if self.host:
            result["host"] = self.host
        if self.host_type:
            result["host_type"] = self.host_type
        if self.port:
            result["port"] = self.port
        if self.registry_prefix:
            result["registry_prefix"] = self.registry_prefix
        if self.resolved_commit:
            result["resolved_commit"] = self.resolved_commit
        if self.resolved_ref:
            result["resolved_ref"] = self.resolved_ref
        if self.version:
            result["version"] = self.version
        if self.virtual_path:
            result["virtual_path"] = self.virtual_path
        if self.is_virtual:
            result["is_virtual"] = self.is_virtual
        if self.depth != 1:
            result["depth"] = self.depth
        if self.resolved_by:
            result["resolved_by"] = self.resolved_by
        if self.package_type:
            result["package_type"] = self.package_type
        if self.deployed_files:
            result["deployed_files"] = sorted(_dedupe_preserving_order(self.deployed_files))
        if self.deployed_file_hashes:
            result["deployed_file_hashes"] = dict(sorted(self.deployed_file_hashes.items()))
        if self.source:
            result["source"] = self.source
        if self.local_path:
            result["local_path"] = self.local_path
        if self.content_hash:
            result["content_hash"] = self.content_hash
        if self.is_dev:
            result["is_dev"] = True
        if self.discovered_via:
            result["discovered_via"] = self.discovered_via
        if self.marketplace_plugin_name:
            result["marketplace_plugin_name"] = self.marketplace_plugin_name
        if self.source_url:
            result["source_url"] = self.source_url
        if self.source_digest:
            result["source_digest"] = self.source_digest
        if self.is_insecure:
            result["is_insecure"] = True
        if self.allow_insecure:
            result["allow_insecure"] = True
        if self.skill_subset:
            result["skill_subset"] = sorted(self.skill_subset)
        if self.target_subset:
            result["target_subset"] = sorted(self.target_subset)
        if self.resolved_url:
            result["resolved_url"] = self.resolved_url
        if self.resolved_hash:
            result["resolved_hash"] = self.resolved_hash
        if self.constraint:
            result["constraint"] = self.constraint
        if self.resolved_tag:
            result["resolved_tag"] = self.resolved_tag
        if self.resolved_at:
            result["resolved_at"] = self.resolved_at
        if self.declared_license:
            result["declared_license"] = self.declared_license
        if self.exec_status:
            result["exec_status"] = self.exec_status
        # Replay forward-compat unknown fields LAST so they never shadow a
        # known field that this build understands.
        for k, v in self._unknown_fields.items():
            result.setdefault(k, v)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockedDependency:
        """Deserialize from dict.

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
        exec_status = _normalize_exec_status(data.get("exec_status"))

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
            "target_subset",
            "resolved_url",
            "resolved_hash",
            "constraint",
            "resolved_tag",
            "resolved_at",
            "declared_license",
            "exec_status",
            "name",
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
            target_subset=list(data.get("target_subset") or []),
            resolved_url=data.get("resolved_url"),
            resolved_hash=data.get("resolved_hash"),
            constraint=data.get("constraint"),
            resolved_tag=data.get("resolved_tag"),
            resolved_at=data.get("resolved_at"),
            declared_license=data.get("declared_license"),
            exec_status=exec_status,
            name=data.get("name"),
            _unknown_fields=unknown_fields,
        )

    @classmethod
    def from_dependency_ref(
        cls,
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
        """Create from a DependencyReference with resolution info.

        Args:
            dep_ref: The resolved dependency reference.
            resolved_commit: Exact commit SHA that was installed, or ``None``.
            depth: Dependency tree depth.
            resolved_by: Parent repo URL, or ``None`` for direct dependencies.
            is_dev: Whether this is a dev-only dependency.
            registry_config: Optional :class:`~apm_cli.deps.registry_proxy.RegistryConfig`
                used for this download (Artifactory VCS proxy — pre-existing
                concept, distinct from the new dedicated-registry resolver).
                When provided, ``host`` is set to the pure FQDN and
                ``registry_prefix`` to the URL path prefix.
            registry_resolution: Optional :class:`~apm_cli.deps.registry.resolver.RegistryResolution`
                produced by the dedicated-registry resolver. When provided,
                ``source`` is set to ``"registry"`` and ``resolved_url`` /
                ``resolved_hash`` / ``version`` are populated from it (the
                trust anchor for re-installs per design §6.1).
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

    def to_dependency_ref(self) -> DependencyReference:
        """Reconstruct a DependencyReference from this locked dependency.

        Registry-sourced deps come back with ``source="registry"`` so the
        install pipeline routes them to the registry resolver. The exact
        locked version is in ``reference`` (the registry resolver still calls
        /versions and the hash-check on download enforces the lockfile's
        intent).
        """
        # Registry deps: prefer the locked exact version over resolved_ref so
        # the resolver picks up the exact-version constraint, not the original
        # range (e.g. ``^1.2.0`` -> ``1.5.3``).
        is_registry = self.source == "registry"
        ref = self.version if (is_registry and self.version) else self.resolved_ref
        return DependencyReference(
            repo_url=self.repo_url,
            host=self.host,
            host_type=self.host_type,
            port=self.port,
            reference=ref,
            virtual_path=self.virtual_path,
            is_virtual=self.is_virtual,
            artifactory_prefix=self.registry_prefix,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            is_insecure=self.is_insecure,
            allow_insecure=self.allow_insecure,
            source=self.source,
            target_subset=sorted(self.target_subset) if self.target_subset else None,
        )


@dataclass
class LockFile:
    """APM lock file for reproducible dependency resolution."""

    lockfile_version: str = "1"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    apm_version: str | None = None
    dependencies: dict[str, LockedDependency] = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    mcp_configs: dict[str, dict] = field(default_factory=dict)
    mcp_target_servers: dict[str, list[str]] = field(default_factory=dict)
    # Provenance for transitively-contributed MCP servers: name -> declaring
    # package identity. Only servers NOT declared in the root manifest's mcp:
    # block appear here (absent == direct, mirroring the dependency-side
    # ``resolved_by is None`` convention). Kept OUT of ``mcp_configs`` values so
    # it never pollutes config comparisons. Consistency diagnostics use this as
    # ownership context only; provenance never exempts a lock-only server.
    mcp_config_provenance: dict[str, str] = field(default_factory=dict)
    lsp_servers: list[str] = field(default_factory=list)
    lsp_configs: dict[str, dict] = field(default_factory=dict)
    local_deployed_files: list[str] = field(default_factory=list)
    local_deployed_file_hashes: dict[str, str] = field(default_factory=dict)

    def add_dependency(self, dep: LockedDependency) -> None:
        """Add a dependency to the lock file.

        Adding a registry-sourced dep or a git-source dep with semver
        resolution fields promotes ``lockfile_version`` to ``"2"`` eagerly,
        keeping the in-memory state consistent with what ``to_yaml()``
        would emit (design section 6.1; issue #1488).
        """
        dep.deployed_files = _dedupe_preserving_order(dep.deployed_files)
        self.dependencies[dep.get_unique_key()] = dep
        if self.lockfile_version == "1" and (
            dep.source == "registry" or dep.constraint or dep.resolved_tag or dep.resolved_at
        ):
            self.lockfile_version = "2"

    def get_dependency(self, key: str) -> LockedDependency | None:
        """Get a dependency by its unique key."""
        return self.dependencies.get(key)

    def has_dependency(self, key: str) -> bool:
        """Check if a dependency exists."""
        return key in self.dependencies

    def get_all_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies sorted by depth then repo_url."""
        return sorted(self.dependencies.values(), key=lambda d: (d.depth, d.repo_url))

    def get_package_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies excluding the virtual self-entry."""
        return [d for d in self.get_all_dependencies() if d.local_path != "."]

    def _needs_v2(self) -> bool:
        """Whether the resolved graph requires lockfile schema v2.

        Per design section 6.1 (and invariant 2.1.4): bump opportunistically --
        only when at least one dep is sourced from a dedicated registry, OR
        when at least one dep carries git-source semver resolution fields
        (``constraint`` / ``resolved_tag`` / ``resolved_at`` -- issue #1488).
        A project that uses neither feature keeps v1 forever, even on a
        newer client.
        """
        for d in self.dependencies.values():
            if d.source == "registry":
                return True
            if d.constraint or d.resolved_tag or d.resolved_at:
                return True
        return False

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        # Opportunistic v1<->v2 derivation (design §6.1, invariant §2.1.4):
        # the lockfile_version field always reflects current content at
        # emit time. ``add_dependency`` bumps to "2" eagerly, but callers
        # that mutate ``self.dependencies`` directly or remove the last
        # registry / git-semver dep need the field re-derived here so the
        # on-disk version is correct in both directions.
        self.lockfile_version = "2" if self._needs_v2() else "1"
        emit_version = self.lockfile_version
        # The synthesized self-entry (key ".") is an in-memory normalization
        # of the flat local_deployed_files / local_deployed_file_hashes
        # fields. It must not be written back into the dependencies list,
        # since the flat fields remain the source of truth in YAML.
        _self_dep = self.dependencies.pop(_SELF_KEY, None)
        try:
            data: dict[str, Any] = {
                "lockfile_version": emit_version,
                "generated_at": self.generated_at,
            }
            if self.apm_version:
                data["apm_version"] = self.apm_version
            data["dependencies"] = [dep.to_dict() for dep in self.get_all_dependencies()]
            if self.mcp_servers:
                data["mcp_servers"] = sorted(self.mcp_servers)
            if self.mcp_configs:
                data["mcp_configs"] = dict(sorted(self.mcp_configs.items()))
            if self.mcp_target_servers:
                data["mcp_target_servers"] = {
                    target: sorted(servers)
                    for target, servers in sorted(self.mcp_target_servers.items())
                }
            if self.mcp_config_provenance:
                data["mcp_config_provenance"] = dict(sorted(self.mcp_config_provenance.items()))
            if self.lsp_servers:
                data["lsp_servers"] = sorted(self.lsp_servers)
            if self.lsp_configs:
                data["lsp_configs"] = dict(sorted(self.lsp_configs.items()))
            if self.local_deployed_files:
                data["local_deployed_files"] = sorted(self.local_deployed_files)
            if self.local_deployed_file_hashes:
                data["local_deployed_file_hashes"] = dict(
                    sorted(self.local_deployed_file_hashes.items())
                )
            from ..utils.yaml_io import yaml_to_str

            return yaml_to_str(data)
        finally:
            if _self_dep is not None:
                self.dependencies[_SELF_KEY] = _self_dep

    @classmethod
    def from_yaml(cls, yaml_str: str) -> LockFile:
        """Deserialize from YAML string."""
        from ..utils.yaml_io import load_yaml_str

        # Bounded loader: an untrusted bundle apm.lock.yaml cannot wedge the
        # parser with a merge-key bomb (the surrounding LockFile.read guard
        # cannot catch a non-terminating safe_load loop -- it can catch the
        # YAMLError this raises instead).
        data = load_yaml_str(yaml_str)
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        lock = cls(
            lockfile_version=data.get("lockfile_version", "1"),
            generated_at=data.get("generated_at", ""),
            apm_version=data.get("apm_version"),
        )
        for dep_data in data.get("dependencies", []):
            lock.add_dependency(LockedDependency.from_dict(dep_data))
        lock.mcp_servers = list(data.get("mcp_servers", []))
        lock.mcp_configs = dict(data.get("mcp_configs") or {})
        lock.mcp_target_servers = {
            target: list(servers)
            for target, servers in (data.get("mcp_target_servers") or {}).items()
        }
        lock.mcp_config_provenance = dict(data.get("mcp_config_provenance") or {})
        lock.lsp_servers = list(data.get("lsp_servers", []))
        lock.lsp_configs = dict(data.get("lsp_configs") or {})
        lock.local_deployed_files = list(data.get("local_deployed_files", []))
        lock.local_deployed_file_hashes = dict(data.get("local_deployed_file_hashes") or {})
        # Synthesize a virtual self-entry representing the project's own
        # local content. This unifies traversal across "real" dependencies
        # and the local package, without changing the on-disk YAML shape.
        if lock.local_deployed_files:
            lock.dependencies[_SELF_KEY] = LockedDependency(
                repo_url="<self>",
                source="local",
                local_path=".",
                is_dev=True,
                depth=0,
                deployed_files=list(lock.local_deployed_files),
                deployed_file_hashes=dict(lock.local_deployed_file_hashes),
            )
        return lock

    def write(self, path: Path) -> None:
        """Write lock file to disk."""
        path.write_text(self.to_yaml(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> LockFile | None:
        """Read lock file from disk. Returns None if not exists or corrupt."""
        if not path.exists():
            return None
        try:
            return cls.from_yaml(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, ValueError, KeyError):
            return None

    @classmethod
    def load_or_create(cls, path: Path) -> LockFile:
        """Load existing lock file or create a new one."""
        return cls.read(path) or cls()

    @classmethod
    def from_installed_packages(
        cls,
        installed_packages,
        dependency_graph,
    ) -> LockFile:
        """Create a lock file from installed packages.

        Args:
            installed_packages: List of
                :class:`~apm_cli.deps.installed_package.InstalledPackage`
                objects **or** legacy tuples of the form
                ``(dep_ref, resolved_commit, depth, resolved_by[, is_dev])``.
                The 5th tuple element is optional for backward compatibility.
            dependency_graph: The resolved DependencyGraph for additional metadata.
        """
        from .installed_package import InstalledPackage

        # Get APM version
        try:
            from importlib.metadata import version

            apm_version = version("apm-cli")
        except Exception:
            apm_version = "unknown"

        lock = cls(apm_version=apm_version)

        for entry in installed_packages:
            registry_resolution = None
            git_semver_resolution = None
            if isinstance(entry, InstalledPackage):
                dep_ref = entry.dep_ref
                resolved_commit = entry.resolved_commit
                depth = entry.depth
                resolved_by = entry.resolved_by
                is_dev = entry.is_dev
                registry_config = getattr(entry, "registry_config", None)
                registry_resolution = getattr(entry, "registry_resolution", None)
                git_semver_resolution = getattr(entry, "git_semver_resolution", None)
            elif len(entry) >= 5:
                dep_ref, resolved_commit, depth, resolved_by, is_dev = entry[:5]
                registry_config = None
            else:
                dep_ref, resolved_commit, depth, resolved_by = entry[:4]
                is_dev = False
                registry_config = None

            locked_dep = LockedDependency.from_dependency_ref(
                dep_ref=dep_ref,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=is_dev,
                registry_config=registry_config,
                registry_resolution=registry_resolution,
                git_semver_resolution=git_semver_resolution,
                package_name=getattr(entry, "package_name", None),
                package_version=getattr(entry, "package_version", None),
            )
            lock.add_dependency(locked_dep)

        return lock

    def get_installed_paths(self, apm_modules_dir: Path) -> list[str]:
        """Get relative installed paths for all dependencies in this lockfile.

        Computes expected installed paths for all dependencies, including
        transitive ones. Used by:
        - Primitive discovery to find all dependency primitives
        - Orphan detection to avoid false positives for transitive deps

        Args:
            apm_modules_dir: Path to the apm_modules directory.

        Returns:
            List[str]: POSIX-style relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        seen: set = set()
        paths: list[str] = []
        for dep in self.get_all_dependencies():
            if dep.local_path == _SELF_KEY:
                continue
            dep_ref = dep.to_dependency_ref()
            install_path = dep_ref.get_install_path(apm_modules_dir)
            try:
                rel_path = install_path.relative_to(apm_modules_dir).as_posix()
            except ValueError:
                rel_path = Path(install_path).as_posix()
            if rel_path not in seen:
                seen.add(rel_path)
                paths.append(rel_path)
        return paths

    def save(self, path: Path) -> None:
        """Save lock file to disk (alias for write)."""
        self.write(path)

    def is_semantically_equivalent(self, other: LockFile) -> bool:
        """Return True if *other* has the same deps, MCP/LSP servers, and configs.

        Ignores ``generated_at`` and ``apm_version`` so that a no-change
        install does not dirty the lockfile.
        """
        if self.lockfile_version != other.lockfile_version:
            return False
        if set(self.dependencies.keys()) != set(other.dependencies.keys()):
            return False
        for key, dep in self.dependencies.items():
            other_dep = other.dependencies[key]
            if dep.to_dict() != other_dep.to_dict():
                return False
        if sorted(self.mcp_servers) != sorted(other.mcp_servers):
            return False
        if self.mcp_configs != other.mcp_configs:
            return False
        if self.mcp_target_servers != other.mcp_target_servers:
            return False
        if self.mcp_config_provenance != other.mcp_config_provenance:
            return False
        if sorted(self.lsp_servers) != sorted(other.lsp_servers):
            return False
        if self.lsp_configs != other.lsp_configs:
            return False
        if sorted(self.local_deployed_files) != sorted(other.local_deployed_files):
            return False
        # Issue #887: include hash dict in equivalence so post-install
        # hash updates persist even when the file list is unchanged.
        if dict(self.local_deployed_file_hashes) != dict(other.local_deployed_file_hashes):  # noqa: SIM103
            return False
        return True

    @classmethod
    def installed_paths_for_project(cls, project_root: Path) -> list[str]:
        """Load apm.lock.yaml from project_root and return installed paths.

        Returns an empty list if the lockfile is missing, corrupt, or
        unreadable.

        Args:
            project_root: Path to project root containing apm.lock.yaml.

        Returns:
            List[str]: Relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        try:
            lockfile_path = get_lockfile_path(project_root)
            if not lockfile_path.exists():
                # Fallback to legacy lockfile for pre-migration reads
                legacy_path = project_root / LEGACY_LOCKFILE_NAME
                if legacy_path.exists():
                    lockfile_path = legacy_path
            lockfile = cls.read(lockfile_path)
            if not lockfile:
                return []
            return lockfile.get_installed_paths(project_root / "apm_modules")
        except (FileNotFoundError, yaml.YAMLError, ValueError, KeyError):
            return []


# Current lockfile filename (with .yaml extension for IDE syntax highlighting)
LOCKFILE_NAME = "apm.lock.yaml"
# Legacy lockfile filename used in older APM versions
LEGACY_LOCKFILE_NAME = "apm.lock"


def get_lockfile_path(project_root: Path) -> Path:
    """Get the path to the lock file for a project."""
    return project_root / LOCKFILE_NAME


def migrate_lockfile_if_needed(project_root: Path) -> bool:
    """Migrate legacy apm.lock to apm.lock.yaml if needed.

    Renames ``apm.lock`` to ``apm.lock.yaml`` when the new file does not yet
    exist.  This is a one-time, transparent migration for users upgrading from
    older APM versions.

    Args:
        project_root: Path to the project root directory.

    Returns:
        True if a migration was performed, False otherwise.
    """
    new_path = get_lockfile_path(project_root)
    legacy_path = project_root / LEGACY_LOCKFILE_NAME
    if not new_path.exists() and legacy_path.exists():
        try:
            legacy_path.rename(new_path)
        except OSError:
            logger.debug("Could not rename %s to %s", legacy_path, new_path, exc_info=True)
            return False
        return True
    return False


def get_lockfile_installed_paths(project_root: Path) -> list[str]:
    """Deprecated: use LockFile.installed_paths_for_project() instead."""
    return LockFile.installed_paths_for_project(project_root)
