"""LockedDependency dataclass for APM lock file entries.

Extracted from :mod:`apm_cli.deps.lockfile` to keep individual modules
within the project's 500-line budget.  All public consumers should continue
to import from ``apm_cli.deps.lockfile``; this module is private.
"""

from dataclasses import dataclass, field
from typing import Any

from ..models.apm_package import DependencyReference


@dataclass(frozen=True, slots=True)
class _DepResolutionInfo:
    """Bundled resolution info for :meth:`LockedDependency.from_dependency_ref`."""

    resolved_commit: str | None
    depth: int
    resolved_by: str | None
    is_dev: bool = False
    registry_config: Any = None


@dataclass
class LockedDependency:
    """A resolved dependency with exact commit/version information."""

    repo_url: str
    host: str | None = None
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
    local_path: str | None = None  # Original local path (relative to project root)
    content_hash: str | None = None  # SHA-256 of package file tree
    is_dev: bool = False  # True for devDependencies
    discovered_via: str | None = None  # Marketplace name (provenance)
    marketplace_plugin_name: str | None = None  # Plugin name in marketplace
    is_insecure: bool = False  # True when the locked source was http://
    allow_insecure: bool = False  # True when the manifest explicitly allowed HTTP
    skill_subset: list[str] = field(default_factory=list)  # Sorted skill names for SKILL_BUNDLE

    def get_unique_key(self) -> str:
        """Returns unique key for this dependency."""
        if self.source == "local" and self.local_path:
            return self.local_path
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url

    # Simple string/scalar fields that are emitted only when truthy (non-empty)
    _OPTIONAL_SCALAR_FIELDS: tuple = (
        "host",
        "port",
        "registry_prefix",
        "resolved_commit",
        "resolved_ref",
        "version",
        "virtual_path",
        "resolved_by",
        "package_type",
        "source",
        "local_path",
        "content_hash",
        "discovered_via",
        "marketplace_plugin_name",
    )

    def _serialize_optional_scalars(self, result: dict[str, Any]) -> None:
        """Populate *result* with all truthy scalar fields (avoids repeating if-blocks)."""
        for key in self._OPTIONAL_SCALAR_FIELDS:
            val = getattr(self, key)
            if val:
                result[key] = val

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for YAML output."""
        result: dict[str, Any] = {"repo_url": self.repo_url}
        self._serialize_optional_scalars(result)
        if self.is_virtual:
            result["is_virtual"] = self.is_virtual
        if self.depth != 1:
            result["depth"] = self.depth
        if self.deployed_files:
            result["deployed_files"] = sorted(self.deployed_files)
        if self.deployed_file_hashes:
            result["deployed_file_hashes"] = dict(sorted(self.deployed_file_hashes.items()))
        if self.is_dev:
            result["is_dev"] = True
        if self.is_insecure:
            result["is_insecure"] = True
        if self.allow_insecure:
            result["allow_insecure"] = True
        if self.skill_subset:
            result["skill_subset"] = sorted(self.skill_subset)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockedDependency":
        """Deserialize from dict.

        Handles backwards compatibility:
        - Old ``deployed_skills`` lists are migrated to ``deployed_files``
          paths under ``.github/skills/`` and ``.claude/skills/``.
        """
        deployed_files = list(data.get("deployed_files", []))

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

        return cls(
            repo_url=data["repo_url"],
            host=data.get("host"),
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
            is_insecure=data.get("is_insecure", False),
            allow_insecure=data.get("allow_insecure", False),
            skill_subset=list(data.get("skill_subset") or []),
        )

    @classmethod
    def from_dependency_ref(
        cls,
        dep_ref: DependencyReference,
        resolution: _DepResolutionInfo,
    ) -> "LockedDependency":
        """Create from a DependencyReference with resolution info.

        Args:
            dep_ref: The resolved dependency reference.
            resolution: Bundled resolution metadata (commit, depth, resolver,
                dev-flag, and optional registry config).  When
                ``resolution.registry_config`` is provided, ``host`` is set to
                the pure FQDN (e.g. ``"art.example.com"``) and
                ``registry_prefix`` is set to the URL path prefix (e.g.
                ``"artifactory/github"``), ensuring correct auth routing on
                subsequent installs.
        """
        if resolution.registry_config is not None:
            host = resolution.registry_config.host
            registry_prefix = resolution.registry_config.prefix
        else:
            host = dep_ref.host
            registry_prefix = None
        return cls(
            repo_url=dep_ref.repo_url,
            host=host,
            port=dep_ref.port,
            registry_prefix=registry_prefix,
            resolved_commit=resolution.resolved_commit,
            resolved_ref=dep_ref.reference,
            virtual_path=dep_ref.virtual_path,
            is_virtual=dep_ref.is_virtual,
            depth=resolution.depth,
            resolved_by=resolution.resolved_by,
            source="local" if dep_ref.is_local else None,
            local_path=dep_ref.local_path if dep_ref.is_local else None,
            is_dev=resolution.is_dev,
            is_insecure=dep_ref.is_insecure,
            allow_insecure=dep_ref.allow_insecure,
            skill_subset=sorted(dep_ref.skill_subset)
            if isinstance(getattr(dep_ref, "skill_subset", None), list)
            else [],
        )

    def to_dependency_ref(self) -> DependencyReference:
        """Reconstruct a DependencyReference from this locked dependency."""
        return DependencyReference(
            repo_url=self.repo_url,
            host=self.host,
            port=self.port,
            reference=self.resolved_ref,
            virtual_path=self.virtual_path,
            is_virtual=self.is_virtual,
            artifactory_prefix=self.registry_prefix,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            is_insecure=self.is_insecure,
            allow_insecure=self.allow_insecure,
        )
