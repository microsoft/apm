"""Transport-agnostic APM package requirement model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...utils.path_security import ensure_path_within, validate_path_segments


@dataclass
class PackageRequirement:
    """Logical APM package requirement resolved through configured repositories."""

    name: str
    version: Optional[str] = None
    repository: Optional[str] = None
    alias: Optional[str] = None

    # Compatibility surface for existing install/lockfile code
    is_local: bool = False
    local_path: Optional[str] = None
    is_virtual: bool = False
    virtual_path: Optional[str] = None
    host: Optional[str] = None

    # Populated by repository resolution
    resolved_source_type: Optional[str] = None
    resolved_repository: Optional[str] = None
    resolved_ref: Optional[str] = None
    resolved_digest: Optional[str] = None
    resolved_host: Optional[str] = None

    dependency_kind: str = "package_requirement"

    @property
    def repo_url(self) -> str:
        """Compatibility alias used throughout the existing codebase."""
        return self.name

    @property
    def reference(self) -> Optional[str]:
        """Compatibility alias for version/ref-style pins."""
        return self.version

    @classmethod
    def parse(cls, raw: str) -> "PackageRequirement":
        """Parse a shorthand logical requirement like ``owner/repo#v1.2.0``."""
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Package requirement must be a non-empty string")
        value = raw.strip()
        name, version = value, None
        if "#" in value:
            name, version = value.rsplit("#", 1)
            version = version.strip() or None
        name = name.strip().strip("/")
        if not name or "/" not in name:
            raise ValueError("Package requirement must be in 'owner/repo' form")
        validate_path_segments(name, context="package name")
        return cls(name=name, version=version)

    @classmethod
    def from_dict(cls, entry: dict) -> "PackageRequirement":
        """Parse object-style logical dependency entry."""
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Package dependency 'name' must be a non-empty string")
        validate_path_segments(name.strip().strip("/"), context="package name")

        version = entry.get("version")
        if version is not None:
            if not isinstance(version, str) or not version.strip():
                raise ValueError("Package dependency 'version' must be a non-empty string")
            version = version.strip()

        repository = entry.get("repository")
        if repository is not None:
            if not isinstance(repository, str) or not repository.strip():
                raise ValueError("Package dependency 'repository' must be a non-empty string")
            repository = repository.strip()

        alias = entry.get("alias")
        if alias is not None:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("Package dependency 'alias' must be a non-empty string")
            alias = alias.strip()

        return cls(
            name=name.strip().strip("/"),
            version=version,
            repository=repository,
            alias=alias,
        )

    def get_unique_key(self) -> str:
        """Return a stable identity for duplicate detection and locking."""
        return self.name

    def get_identity(self) -> str:
        """Return logical package identity without resolved transport details."""
        return self.name

    def get_display_name(self) -> str:
        """Return a human-readable package name."""
        return self.alias or self.name

    def get_install_path(self, apm_modules_dir: Path) -> Path:
        """Compute install path from the logical package name."""
        validate_path_segments(self.name, context="package name")
        result = apm_modules_dir.joinpath(*self.name.split("/"))
        ensure_path_within(result, apm_modules_dir)
        return result

    def __str__(self) -> str:
        result = self.name
        if self.version:
            result += f"#{self.version}"
        return result
