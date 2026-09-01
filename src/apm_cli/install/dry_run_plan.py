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
    selected_apm_dependencies: tuple[DependencyReference, ...]
    mcp_dependencies: tuple[Any, ...]
    should_install_apm: bool
    should_install_mcp: bool
    only_packages: tuple[str, ...] | None
    updated_apm_identities: frozenset[str] = frozenset()

    @classmethod
    def from_apm_package(
        cls,
        apm_package: Any,
        *,
        should_install_apm: bool,
        should_install_mcp: bool,
        only_packages: Sequence[str] | None,
        updated_packages: Sequence[str] = (),
    ) -> ProspectiveInstallPlan:
        """Build the preview from one interpreted prospective package."""
        apm_dependencies = tuple(apm_package.get_apm_dependencies())
        dev_apm_dependencies = tuple(apm_package.get_dev_apm_dependencies())
        all_apm_dependencies = apm_dependencies + dev_apm_dependencies
        selected_apm_dependencies = all_apm_dependencies
        if only_packages is not None:
            selected_identities = {
                DependencyReference.parse(package).get_identity() for package in only_packages
            }
            selected_apm_dependencies = tuple(
                dependency
                for dependency in all_apm_dependencies
                if dependency.get_identity() in selected_identities
            )
        return cls(
            apm_dependencies=apm_dependencies,
            dev_apm_dependencies=dev_apm_dependencies,
            selected_apm_dependencies=selected_apm_dependencies,
            mcp_dependencies=tuple(apm_package.get_all_mcp_dependencies()),
            should_install_apm=should_install_apm,
            should_install_mcp=should_install_mcp,
            only_packages=tuple(only_packages) if only_packages is not None else None,
            updated_apm_identities=frozenset(
                DependencyReference.parse(package).get_identity() for package in updated_packages
            ),
        )

    @property
    def all_apm_dependencies(self) -> tuple[DependencyReference, ...]:
        """Return every APM dependency that the prospective install contains."""
        return self.apm_dependencies + self.dev_apm_dependencies

    @property
    def apm_dependency_count(self) -> int:
        """Return the number of APM dependencies selected for preview."""
        return len(self.selected_apm_dependencies) if self.should_install_apm else 0

    @property
    def mcp_dependency_count(self) -> int:
        """Return the number of MCP dependencies selected for preview."""
        return len(self.selected_mcp_dependencies)

    @property
    def selected_mcp_dependencies(self) -> tuple[Any, ...]:
        """Return MCP dependencies only when the invocation selected MCP."""
        return self.mcp_dependencies if self.should_install_mcp else ()

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
