"""Dependency sources -- Strategy pattern for the install pipeline.

Each ``DependencySource`` knows how to *acquire* one dependency: bring its
files onto disk, build a ``PackageInfo``, register it in the lockfile-bound
state, and return the metadata the integration template needs.

After ``acquire()``, all sources flow through the same template
(``apm_cli.install.template.run_integration_template``) which handles the
security gate, primitive integration, and per-package diagnostics.

This module deliberately contains *only* source-specific logic.  Anything
shared across sources lives in the template.

Sources
-------
- ``LocalDependencySource``: ``file://`` deps copied from the workspace.
- ``CachedDependencySource``: deps already extracted in ``apm_modules/``.
- ``FreshDependencySource``: deps that need a network download (with
  supply-chain hash verification on top of the existing lockfile entry).

The root-project integration (``<project_root>/.apm/``) follows a
substantially different shape (no PackageInfo, dedicated tracking on
``ctx.local_deployed_files``) and is handled separately in
``phases/integrate.py``.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from apm_cli.utils.console import _rich_error, _rich_success

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import PackageInfo


def _format_package_type_label(pkg_type) -> Optional[str]:
    """Human-readable label for a detected ``PackageType``.

    Centralised so every install path emits the same wording and so
    new ``PackageType`` values can be added without grepping for ad-hoc
    dicts.  Missing ``HOOK_PACKAGE`` from this table is what made
    microsoft/apm#780 silent -- keep all classifiable enum members
    covered.
    """
    from apm_cli.models.apm_package import PackageType

    return {
        PackageType.CLAUDE_SKILL: "Skill (SKILL.md detected)",
        PackageType.MARKETPLACE_PLUGIN:
            "Marketplace Plugin (plugin.json or agents/skills/commands)",
        PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
        PackageType.APM_PACKAGE: "APM Package (apm.yml)",
        PackageType.HOOK_PACKAGE: "Hook Package (hooks/*.json only)",
        PackageType.SKILL_BUNDLE: "Skill Bundle (skills/<name>/SKILL.md)",
    }.get(pkg_type)


@dataclass
class Materialization:
    """Outcome of ``DependencySource.acquire()``.

    Carries everything the integration template needs to run the security
    gate + primitive integration on a freshly-acquired package.
    """

    package_info: Optional["PackageInfo"]
    install_path: Path
    dep_key: str
    deltas: Dict[str, int] = field(default_factory=lambda: {"installed": 1})


class DependencySource(ABC):
    """Strategy: acquire one dependency and prepare it for integration.

    Subclasses encapsulate source-specific concerns (filesystem copy,
    cache reuse, fresh download with progress + hash verification).
    The post-acquire template flow is the same for every source.
    """

    INTEGRATE_ERROR_PREFIX: str = "Failed to integrate primitives"
    """Per-source error wording used by the integration template when
    ``integrate_package_primitives`` raises.  Subclasses override to
    preserve the legacy diagnostic text shown to users."""

    def __init__(
        self,
        ctx: "InstallContext",
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
    ):
        self.ctx = ctx
        self.dep_ref = dep_ref
        self.install_path = install_path
        self.dep_key = dep_key

    @abstractmethod
    def acquire(self) -> Optional[Materialization]:
        """Materialise the dependency on disk and build PackageInfo.

        Returns ``None`` to skip integration entirely (e.g. local dep at
        user scope, copy/download failure).  Otherwise returns a
        ``Materialization`` consumed by the integration template.
        """


class LocalDependencySource(DependencySource):
    """Local (``file://``) dependency: copy from a filesystem path."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from local package"

    def acquire(self) -> Optional[Materialization]:
        from apm_cli.core.scope import InstallScope
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.install.phases.local_content import _copy_local_package
        from apm_cli.models.apm_package import (
            APMPackage,
            GitReferenceType,
            PackageInfo,
            PackageType,
            ResolvedReference,
        )
        from apm_cli.models.validation import detect_package_type
        from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

        ctx = self.ctx
        dep_ref = self.dep_ref
        install_path = self.install_path
        dep_key = self.dep_key
        diagnostics = ctx.diagnostics
        logger = ctx.logger

        # User scope: relative paths would resolve against $HOME instead
        # of cwd, producing wrong results.  Skip with a clear diagnostic.
        if ctx.scope is InstallScope.USER:
            diagnostics.warn(
                f"Skipped local package '{dep_ref.local_path}' "
                "-- local paths are not supported at user scope (--global). "
                "Use a remote reference (owner/repo) instead.",
                package=dep_ref.local_path,
            )
            if logger:
                logger.verbose_detail(
                    f"  Skipping {dep_ref.local_path} (local packages "
                    "resolve against cwd, not $HOME)"
                )
            return None

        result_path = _copy_local_package(
            dep_ref, install_path, ctx.project_root, logger=logger
        )
        if not result_path:
            diagnostics.error(
                f"Failed to copy local package: {dep_ref.local_path}",
                package=dep_ref.local_path,
            )
            return None

        if logger:
            logger.download_complete(dep_ref.local_path, ref_suffix="local")

        # Build minimal PackageInfo for integration
        local_apm_yml = install_path / "apm.yml"
        if local_apm_yml.exists():
            local_pkg = APMPackage.from_apm_yml(local_apm_yml)
            if not local_pkg.source:
                local_pkg.source = dep_ref.local_path
        else:
            local_pkg = APMPackage(
                name=Path(dep_ref.local_path).name,
                version="0.0.0",
                package_path=install_path,
                source=dep_ref.local_path,
            )

        local_ref = ResolvedReference(
            original_ref="local",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="local",
            ref_name="local",
        )
        local_info = PackageInfo(
            package=local_pkg,
            install_path=install_path,
            resolved_reference=local_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
        )

        # Detect package type
        pkg_type, plugin_json_path = detect_package_type(install_path)
        local_info.package_type = pkg_type
        if pkg_type == PackageType.MARKETPLACE_PLUGIN:
            from apm_cli.deps.plugin_parser import normalize_plugin_directory
            normalize_plugin_directory(install_path, plugin_json_path)

        # Record for lockfile
        node = ctx.dependency_graph.dependency_tree.get_node(dep_key)
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False
        ctx.installed_packages.append(InstalledPackage(
            dep_ref=dep_ref, resolved_commit=None,
            depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
            registry_config=None,
        ))
        if install_path.is_dir() and not dep_ref.is_local:
            ctx.package_hashes[dep_key] = _compute_hash(install_path)

        if local_info.package_type:
            ctx.package_types[dep_key] = local_info.package_type.value

        return Materialization(
            package_info=local_info,
            install_path=install_path,
            dep_key=dep_key,
        )


class CachedDependencySource(DependencySource):
    """Cached dependency: already extracted under ``apm_modules/``."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from cached package"

    def __init__(
        self,
        ctx: "InstallContext",
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
        resolved_ref: Any,
        dep_locked_chk: Any,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        self.resolved_ref = resolved_ref
        self.dep_locked_chk = dep_locked_chk

    def acquire(self) -> Optional[Materialization]:
        from apm_cli.constants import APM_YML_FILENAME
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.models.apm_package import (
            APMPackage,
            GitReferenceType,
            PackageInfo,
            ResolvedReference,
        )
        from apm_cli.models.validation import detect_package_type
        from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

        ctx = self.ctx
        dep_ref = self.dep_ref
        install_path = self.install_path
        dep_key = self.dep_key
        resolved_ref = self.resolved_ref
        dep_locked_chk = self.dep_locked_chk
        logger = ctx.logger

        display_name = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
        _ref = dep_ref.reference or ""
        _sha = ""
        if (
            dep_locked_chk
            and dep_locked_chk.resolved_commit
            and dep_locked_chk.resolved_commit != "cached"
        ):
            _sha = dep_locked_chk.resolved_commit[:8]
        if logger:
            logger.download_complete(display_name, ref=_ref, sha=_sha, cached=True)

        deltas: Dict[str, int] = {"installed": 1}
        if not dep_ref.reference:
            deltas["unpinned"] = 1

        # Skip integration entirely if no targets.  The template will
        # write the empty deployed_files entry on its own (single source
        # of truth), so we just signal "skip integration" via
        # package_info=None.
        if not ctx.targets:
            return Materialization(
                package_info=None,
                install_path=install_path,
                dep_key=dep_key,
                deltas=deltas,
            )

        # Load package from apm.yml
        apm_yml_path = install_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            cached_package = APMPackage.from_apm_yml(apm_yml_path)
            if not cached_package.source:
                cached_package.source = dep_ref.repo_url
        else:
            cached_package = APMPackage(
                name=dep_ref.repo_url.split("/")[-1],
                version="unknown",
                package_path=install_path,
                source=dep_ref.repo_url,
            )

        resolved_or_cached_ref = resolved_ref if resolved_ref else ResolvedReference(
            original_ref=dep_ref.reference or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="cached",
            ref_name=dep_ref.reference or "default",
        )

        cached_package_info = PackageInfo(
            package=cached_package,
            install_path=install_path,
            resolved_reference=resolved_or_cached_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
        )

        pkg_type, _ = detect_package_type(install_path)
        cached_package_info.package_type = pkg_type

        # Collect for lockfile
        node = ctx.dependency_graph.dependency_tree.get_node(dep_key)
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False

        # Determine commit SHA: resolved > callback > existing lockfile > reference
        cached_commit = None
        if (
            resolved_ref
            and resolved_ref.resolved_commit
            and resolved_ref.resolved_commit != "cached"
        ):
            cached_commit = resolved_ref.resolved_commit
        if not cached_commit:
            cached_commit = ctx.callback_downloaded.get(dep_key)
        if not cached_commit and ctx.existing_lockfile:
            locked_dep = ctx.existing_lockfile.get_dependency(dep_key)
            if locked_dep:
                cached_commit = locked_dep.resolved_commit
        if not cached_commit:
            cached_commit = dep_ref.reference

        # Determine if cached package came from registry
        _cached_registry = None
        if dep_locked_chk and dep_locked_chk.registry_prefix:
            _cached_registry = ctx.registry_config
        elif ctx.registry_config and not dep_ref.is_local:
            _cached_registry = ctx.registry_config

        ctx.installed_packages.append(InstalledPackage(
            dep_ref=dep_ref, resolved_commit=cached_commit,
            depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
            registry_config=_cached_registry,
        ))
        if install_path.is_dir():
            ctx.package_hashes[dep_key] = _compute_hash(install_path)
        if cached_package_info.package_type:
            ctx.package_types[dep_key] = cached_package_info.package_type.value

        return Materialization(
            package_info=cached_package_info,
            install_path=install_path,
            dep_key=dep_key,
            deltas=deltas,
        )


class FreshDependencySource(DependencySource):
    """Fresh dependency: needs a network download.

    Performs supply-chain hash verification (#763) and, on mismatch,
    aborts the entire process via ``sys.exit(1)`` -- this matches the
    legacy behaviour because content drift from the lockfile is treated
    as a possible tampering event.
    """

    # Inherits the default "Failed to integrate primitives" prefix.

    def __init__(
        self,
        ctx: "InstallContext",
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
        resolved_ref: Any,
        dep_locked_chk: Any,
        ref_changed: bool,
        progress: Any,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        self.resolved_ref = resolved_ref
        self.dep_locked_chk = dep_locked_chk
        self.ref_changed = ref_changed
        self.progress = progress

    def acquire(self) -> Optional[Materialization]:
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.drift import build_download_ref
        from apm_cli.models.apm_package import PackageType
        from apm_cli.utils.content_hash import compute_package_hash as _compute_hash
        from apm_cli.utils.path_security import safe_rmtree

        ctx = self.ctx
        dep_ref = self.dep_ref
        install_path = self.install_path
        dep_key = self.dep_key
        dep_locked_chk = self.dep_locked_chk
        ref_changed = self.ref_changed
        progress = self.progress
        diagnostics = ctx.diagnostics
        logger = ctx.logger

        try:
            display_name = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
            short_name = (
                display_name.split("/")[-1] if "/" in display_name else display_name
            )

            task_id = progress.add_task(
                description=f"Fetching {short_name}",
                total=None,
            )

            download_ref = build_download_ref(
                dep_ref,
                ctx.existing_lockfile,
                update_refs=ctx.update_refs,
                ref_changed=ref_changed,
            )

            if dep_key in ctx.pre_download_results:
                package_info = ctx.pre_download_results[dep_key]
            else:
                package_info = ctx.downloader.download_package(
                    download_ref,
                    install_path,
                    progress_task_id=task_id,
                    progress_obj=progress,
                )

            # CRITICAL: hide progress BEFORE printing success to avoid overlap
            progress.update(task_id, visible=False)
            progress.refresh()

            deltas: Dict[str, int] = {"installed": 1}

            resolved = getattr(package_info, "resolved_reference", None)
            if logger:
                _ref = ""
                _sha = ""
                if resolved:
                    _ref = resolved.ref_name if resolved.ref_name else ""
                    _sha = resolved.resolved_commit[:8] if resolved.resolved_commit else ""
                logger.download_complete(display_name, ref=_ref, sha=_sha)
                if ctx.auth_resolver:
                    try:
                        _host = dep_ref.host or "github.com"
                        _org = (
                            dep_ref.repo_url.split("/")[0]
                            if dep_ref.repo_url and "/" in dep_ref.repo_url
                            else None
                        )
                        _ctx = ctx.auth_resolver.resolve(_host, org=_org, port=dep_ref.port)
                        logger.package_auth(_ctx.source, _ctx.token_type or "none")
                    except Exception:
                        pass
            else:
                _ref_suffix = ""
                if resolved:
                    _r = resolved.ref_name if resolved.ref_name else ""
                    _s = resolved.resolved_commit[:8] if resolved.resolved_commit else ""
                    if _r and _s:
                        _ref_suffix = f" #{_r} @{_s}"
                    elif _r:
                        _ref_suffix = f" #{_r}"
                    elif _s:
                        _ref_suffix = f" @{_s}"
                _rich_success(f"[+] {display_name}{_ref_suffix}")

            if not dep_ref.reference:
                deltas["unpinned"] = 1

            # Lockfile bookkeeping
            resolved_commit = None
            if resolved:
                resolved_commit = package_info.resolved_reference.resolved_commit
            node = ctx.dependency_graph.dependency_tree.get_node(dep_key)
            depth = node.depth if node else 1
            resolved_by = (
                node.parent.dependency_ref.repo_url if node and node.parent else None
            )
            _is_dev = node.is_dev if node else False
            ctx.installed_packages.append(InstalledPackage(
                dep_ref=dep_ref, resolved_commit=resolved_commit,
                depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
                registry_config=ctx.registry_config if not dep_ref.is_local else None,
            ))
            if install_path.is_dir():
                ctx.package_hashes[dep_key] = _compute_hash(install_path)

            # Supply-chain protection: verify content hash on fresh
            # downloads when the lockfile already records a hash.
            if (
                not ctx.update_refs
                and dep_locked_chk
                and dep_locked_chk.content_hash
                and dep_key in ctx.package_hashes
            ):
                _fresh_hash = ctx.package_hashes[dep_key]
                if _fresh_hash != dep_locked_chk.content_hash:
                    safe_rmtree(install_path, ctx.apm_modules_dir)
                    _rich_error(
                        f"Content hash mismatch for "
                        f"{dep_key}: "
                        f"expected {dep_locked_chk.content_hash}, "
                        f"got {_fresh_hash}. "
                        "The downloaded content differs from the "
                        "lockfile record. This may indicate a "
                        "supply-chain attack. Use 'apm install "
                        "--update' to accept new content and "
                        "update the lockfile."
                    )
                    sys.exit(1)

            if hasattr(package_info, "package_type") and package_info.package_type:
                ctx.package_types[dep_key] = package_info.package_type.value

            if hasattr(package_info, "package_type"):
                package_type = package_info.package_type
                _type_label = _format_package_type_label(package_type)
                if _type_label and logger:
                    logger.package_type_info(_type_label)

            # If no targets, skip integration but keep deltas
            if not ctx.targets:
                return Materialization(
                    package_info=None,
                    install_path=install_path,
                    dep_key=dep_key,
                    deltas=deltas,
                )

            return Materialization(
                package_info=package_info,
                install_path=package_info.install_path,
                dep_key=dep_key,
                deltas=deltas,
            )

        except Exception as e:
            display_name = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
            # task_id may not exist if progress.add_task failed; guard it.
            try:
                progress.remove_task(task_id)  # type: ignore[name-defined]
            except Exception:
                pass
            diagnostics.error(
                f"Failed to install {display_name}: {e}",
                package=dep_key,
            )
            return None


def make_dependency_source(
    ctx: "InstallContext",
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
    *,
    resolved_ref: Any = None,
    dep_locked_chk: Any = None,
    ref_changed: bool = False,
    skip_download: bool = False,
    progress: Any = None,
) -> DependencySource:
    """Factory: pick the right ``DependencySource`` for *dep_ref*.

    Caller is responsible for resolving the download strategy (cached vs
    fresh) before invoking the factory; the resolved-ref and
    locked-checksum data flow into the appropriate source.
    """
    if dep_ref.is_local and dep_ref.local_path:
        return LocalDependencySource(ctx, dep_ref, install_path, dep_key)
    if skip_download:
        return CachedDependencySource(
            ctx, dep_ref, install_path, dep_key, resolved_ref, dep_locked_chk,
        )
    return FreshDependencySource(
        ctx, dep_ref, install_path, dep_key,
        resolved_ref, dep_locked_chk, ref_changed, progress,
    )
