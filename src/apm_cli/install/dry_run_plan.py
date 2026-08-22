"""Immutable preview state for ``apm install --dry-run``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apm_cli.models.dependency.reference import DependencyReference


@dataclass(frozen=True)
class ProspectiveInstallPlan:
    """Represent the install state that dry-run would use without persisting it."""

    apm_dependencies: tuple[DependencyReference, ...]
    dev_apm_dependencies: tuple[DependencyReference, ...]
    mcp_dependencies: tuple[Any, ...]
    should_install_apm: bool
    should_install_mcp: bool
    only_packages: tuple[str, ...] | None

    @classmethod
    def from_manifest_and_validated_additions(
        cls,
        *,
        apm_dependencies: Sequence[DependencyReference],
        dev_apm_dependencies: Sequence[DependencyReference],
        mcp_dependencies: Sequence[Any],
        validated_additions: Sequence[str],
        additions_are_dev: bool,
        should_install_apm: bool,
        should_install_mcp: bool,
        only_packages: Sequence[str] | None,
    ) -> ProspectiveInstallPlan:
        """Build the preview from manifest dependencies and validated CLI additions."""
        additions = tuple(DependencyReference.parse(package) for package in validated_additions)
        prospective_apm_dependencies = tuple(apm_dependencies)
        prospective_dev_apm_dependencies = tuple(dev_apm_dependencies)
        if additions_are_dev:
            prospective_dev_apm_dependencies += additions
        else:
            prospective_apm_dependencies += additions

        return cls(
            apm_dependencies=prospective_apm_dependencies,
            dev_apm_dependencies=prospective_dev_apm_dependencies,
            mcp_dependencies=tuple(mcp_dependencies),
            should_install_apm=should_install_apm,
            should_install_mcp=should_install_mcp,
            only_packages=tuple(only_packages) if only_packages is not None else None,
        )

    @property
    def all_apm_dependencies(self) -> tuple[DependencyReference, ...]:
        """Return every APM dependency that the prospective install contains."""
        return self.apm_dependencies + self.dev_apm_dependencies

    @property
    def apm_dependency_count(self) -> int:
        """Return the number of APM dependencies selected for preview."""
        return len(self.all_apm_dependencies) if self.should_install_apm else 0

    @property
    def mcp_dependency_count(self) -> int:
        """Return the number of MCP dependencies selected for preview."""
        return len(self.mcp_dependencies) if self.should_install_mcp else 0

    @property
    def intended_dependency_keys(self) -> frozenset[str]:
        """Return the dependency identities used for orphan previewing."""
        keys: set[str] = set()
        for dependency in self.all_apm_dependencies:
            try:
                keys.add(dependency.get_unique_key())
            except AttributeError:
                continue
        return frozenset(keys)
