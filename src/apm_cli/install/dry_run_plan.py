"""Immutable preview state for ``apm install --dry-run``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy
from apm_cli.install.package_selection import (
    selected_dependency_identity,
    selected_subtree_dependencies,
)
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import project_manifest_dependencies
from apm_cli.security.executables import filter_lsp_by_allow_executables

if TYPE_CHECKING:
    from apm_cli.commands.install import InstallContext


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
    lsp_dependencies: tuple[Any, ...] = ()
    updated_apm_identities: frozenset[str] = frozenset()
    resolved_apm_dependencies: tuple[DependencyReference, ...] | None = None
    dependency_preview_errors: tuple[str, ...] = ()

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
                selected_dependency_identity(package, all_apm_dependencies)
                for package in only_packages
            }
            selected_apm_dependencies = tuple(
                dependency
                for dependency in all_apm_dependencies
                if dependency.get_identity() in selected_identities
            )
        if not should_install_apm:
            selected_apm_dependencies = ()
        else:
            selected_keys = {dep.get_unique_key() for dep in selected_apm_dependencies}
            selected_apm_dependencies = tuple(
                dep
                for dep in project_manifest_dependencies(apm_package)
                if dep.get_unique_key() in selected_keys
            )
        return cls(
            apm_dependencies=apm_dependencies,
            dev_apm_dependencies=dev_apm_dependencies,
            selected_apm_dependencies=selected_apm_dependencies,
            mcp_dependencies=tuple(apm_package.get_all_mcp_dependencies()),
            should_install_apm=should_install_apm,
            should_install_mcp=should_install_mcp,
            only_packages=tuple(only_packages) if only_packages is not None else None,
            lsp_dependencies=tuple(apm_package.get_lsp_dependencies()),
            updated_apm_identities=frozenset(
                selected_dependency_identity(package, all_apm_dependencies)
                for package in updated_packages
            ),
        )

    @property
    def all_apm_dependencies(self) -> tuple[DependencyReference, ...]:
        """Return every APM dependency that the prospective install contains."""
        return self.apm_dependencies + self.dev_apm_dependencies

    @classmethod
    def from_install_context(
        cls,
        apm_package: APMPackage,
        ctx: InstallContext,
        *,
        should_install_apm: bool,
        should_install_mcp: bool,
        updated_packages: Sequence[str] = (),
    ) -> ProspectiveInstallPlan:
        """Build the scoped, trust-filtered offline plan for one CLI invocation."""
        from apm_cli.core.scope import get_modules_dir, get_source_root

        source_root = get_source_root(ctx.scope)
        plan = cls.from_apm_package(
            apm_package,
            should_install_apm=should_install_apm,
            should_install_mcp=should_install_mcp,
            only_packages=ctx.only_packages,
            updated_packages=updated_packages,
        ).with_allowed_lsp_dependencies(apm_package, ctx.logger)
        return plan.with_resolved_dependencies(
            apm_package,
            project_root=source_root if source_root != ctx.project_root else ctx.apm_dir,
            apm_modules_dir=get_modules_dir(ctx.scope),
            lockfile_dir=ctx.apm_dir,
            update=RefFreshnessPolicy.for_install_intent(
                update_refs=ctx.update, refresh=ctx.refresh
            ).requires_remote,
            frozen=ctx.frozen,
        )

    def with_resolved_dependencies(
        self,
        apm_package: APMPackage,
        *,
        project_root: Path,
        apm_modules_dir: Path,
        lockfile_dir: Path,
        update: bool,
        frozen: bool = False,
    ) -> ProspectiveInstallPlan:
        """Reuse installation's graph owner without downloading or changing files."""
        if not self.should_install_apm:
            return self
        try:
            lockfile = LockFile.read(get_lockfile_path(lockfile_dir))
        except (OSError, ValueError) as exc:
            return replace(self, dependency_preview_errors=(f"Cannot read lockfile: {exc}",))
        resolver = APMDependencyResolver(
            apm_modules_dir=apm_modules_dir,
            existing_lockfile=lockfile,
            update_refs=update,
            frozen=frozen,
            read_only=True,
        )
        graph = resolver.resolve_dependencies(project_root, root_package=apm_package)
        resolved_dependencies = tuple(graph.flattened_dependencies.get_installation_list())
        dependencies = (
            tuple(
                selected_subtree_dependencies(
                    self.only_packages, graph.dependency_tree, resolved_dependencies
                )
            )
            if self.only_packages is not None
            else resolved_dependencies
        )
        resolved_keys = {dependency.get_unique_key() for dependency in dependencies}
        dependencies += tuple(
            dependency
            for dependency in self.selected_apm_dependencies
            if dependency.get_unique_key() not in resolved_keys
        )
        errors = list(graph.resolution_errors)
        if graph.circular_dependencies:
            errors.append("Circular dependencies prevent a complete install preview")
        return replace(
            self,
            selected_apm_dependencies=dependencies,
            resolved_apm_dependencies=resolved_dependencies,
            dependency_preview_errors=tuple(errors),
        )

    def with_allowed_lsp_dependencies(
        self, apm_package: Any, logger: Any
    ) -> ProspectiveInstallPlan:
        """Apply executable trust filtering to selected LSP dependencies."""
        if not self.should_install_mcp:
            return self
        allow_executables = getattr(apm_package, "allow_executables", None)
        if not isinstance(allow_executables, dict):
            allow_executables = None
        filtered_lsp = tuple(
            filter_lsp_by_allow_executables(
                list(self.lsp_dependencies),
                allow_executables,
                logger,
            )
        )
        return replace(self, lsp_dependencies=filtered_lsp)

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
    def selected_lsp_dependencies(self) -> tuple[Any, ...]:
        """Return LSP dependencies only when the invocation selected services."""
        return self.lsp_dependencies if self.should_install_mcp else ()

    @property
    def lsp_dependency_count(self) -> int:
        """Return the number of LSP dependencies selected for preview."""
        return len(self.selected_lsp_dependencies)

    @property
    def dependency_counts(self) -> tuple[int, int, int]:
        """Return selected APM, MCP, and LSP dependency counts."""
        return (
            self.apm_dependency_count,
            self.mcp_dependency_count,
            self.lsp_dependency_count,
        )

    @property
    def intended_dependency_keys(self) -> frozenset[str]:
        """Return the dependency identities used for orphan previewing."""
        keys: set[str] = set()
        dependencies = (
            self.resolved_apm_dependencies
            if self.resolved_apm_dependencies is not None
            else self.all_apm_dependencies
        )
        for dependency in dependencies:
            try:
                keys.add(dependency.get_unique_key())
            except AttributeError:
                continue
        return frozenset(keys)
