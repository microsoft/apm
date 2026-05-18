"""Local (``file://``) dependency source."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.install.sources._base import DependencySource, Materialization

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _handle_user_scope_local(ctx: InstallContext, dep_ref: Any, diagnostics: Any, logger) -> bool:
    from apm_cli.core.scope import InstallScope

    if ctx.scope is not InstallScope.USER:
        return False
    local_path_str = dep_ref.local_path or ""
    if local_path_str and Path(local_path_str).expanduser().is_absolute():
        return False
    diagnostics.warn(
        f"Skipped local package '{local_path_str}' "
        "-- relative local paths are not supported at user scope "
        "(--global). Use an absolute path or a remote reference "
        "(owner/repo) instead.",
        package=local_path_str,
    )
    if logger:
        logger.verbose_detail(
            f"  Skipping {local_path_str} (relative local paths "
            "are project-relative and have no root at user scope)"
        )
    return True


class LocalDependencySource(DependencySource):
    """Local (``file://``) dependency: copy from a filesystem path."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from local package"

    def acquire(self) -> Materialization | None:
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

        if _handle_user_scope_local(ctx, dep_ref, diagnostics, logger):
            return None

        # Determine the anchor for relative ``local_path`` (#857). For direct
        # deps from the root project this is project_root. For transitive
        # deps declared inside another local package, it is the parent
        # package's source directory -- captured during resolve via
        # ``ctx.dep_base_dirs``.
        base_dir = getattr(ctx, "dep_base_dirs", {}).get(dep_key) or ctx.project_root
        result_path = _copy_local_package(
            dep_ref,
            install_path,
            base_dir,
            project_root=ctx.project_root,
            logger=logger,
        )
        if not result_path:
            diagnostics.error(
                f"Failed to copy local package: {dep_ref.local_path}",
                package=dep_ref.local_path,
            )
            return None

        if logger:
            logger.download_complete(dep_ref.local_path, ref_suffix="local")

        # Build minimal PackageInfo for integration. Anchor source_path on
        # the *original* user source directory (not the apm_modules copy) so
        # any transitive ``../sibling`` dep declared inside this package
        # resolves against where the developer wrote the path (#857).
        local_apm_yml = install_path / "apm.yml"
        if local_apm_yml.exists():
            original_src = Path(dep_ref.local_path).expanduser()
            if not original_src.is_absolute():
                # For TRANSITIVE local deps the relative path is anchored on
                # the parent package's directory (base_dir above), not on
                # the consumer's project root. Reusing base_dir here keeps
                # the source_path stamped on the loaded APMPackage in lock-
                # step with where _copy_local_package actually copied from.
                original_src = (base_dir / original_src).resolve()
            else:
                original_src = original_src.resolve()
            local_pkg = APMPackage.from_apm_yml(local_apm_yml, source_path=original_src)
            # TODO(#940): post-construction mutation of .source has the same
            # cache-poisoning shape as the bug fixed in this PR. Today the
            # cache key is (apm.yml, source_path) so mutating .source is
            # safe, but keep this in mind when reworking the source field.
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
        ctx.installed_packages.append(
            InstalledPackage(
                dep_ref=dep_ref,
                resolved_commit=None,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=_is_dev,
                registry_config=None,
            )
        )
        if install_path.is_dir() and not dep_ref.is_local:
            ctx.package_hashes[dep_key] = _compute_hash(install_path)

        if local_info.package_type:
            ctx.package_types[dep_key] = local_info.package_type.value

        return Materialization(
            package_info=local_info,
            install_path=install_path,
            dep_key=dep_key,
        )
