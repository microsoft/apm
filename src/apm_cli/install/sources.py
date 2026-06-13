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

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.install.registry_wiring import (
    registry_resolution_for_cached_registry_dep,
)
from apm_cli.install.sources_base import DependencySource, Materialization
from apm_cli.install.sources_fresh import (
    FreshDependencySource,
    _format_package_type_label,
)
from apm_cli.utils.short_sha import format_short_sha

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

__all__ = [
    "CachedDependencySource",
    "DependencySource",
    "FreshDependencySource",
    "LocalDependencySource",
    "Materialization",
    "_format_package_type_label",
    "_rebuild_cached_semver_resolution",
    "make_dependency_source",
]


def _rebuild_cached_semver_resolution(dep_locked_chk: Any) -> Any:
    """Rebuild a ``GitSemverResolution`` from a cached lockfile entry.

    Returns ``None`` unless ALL required fields are present on
    *dep_locked_chk*:  ``constraint``, ``version``, ``resolved_tag``,
    and ``resolved_commit``.  Per PR #1496 review thread: gating on
    just ``constraint`` and back-filling missing fields with empty
    strings risks propagating an incomplete semver resolution into
    ``InstalledPackage`` and rewriting the lockfile with empty/missing
    fields (and an empty ``resolved_ref``).  When the lockfile cache is
    incomplete we prefer to leave the resolution as ``None`` so the
    caller falls back to the literal-ref path.
    """
    if dep_locked_chk is None:
        return None
    if not (
        dep_locked_chk.constraint
        and dep_locked_chk.version
        and dep_locked_chk.resolved_tag
        and dep_locked_chk.resolved_commit
    ):
        return None
    from apm_cli.deps.git_semver_resolver import GitSemverResolution

    return GitSemverResolution(
        constraint=dep_locked_chk.constraint,
        resolved_version=dep_locked_chk.version,
        resolved_tag=dep_locked_chk.resolved_tag,
        resolved_sha=dep_locked_chk.resolved_commit,
        matched_pattern="",
        resolved_at=dep_locked_chk.resolved_at or "",
    )


class LocalDependencySource(DependencySource):
    """Local (``file://``) dependency: copy from a filesystem path."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from local package"

    def acquire(self) -> Materialization | None:
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

        # User scope: relative paths are project-relative and have no
        # meaningful root outside a project, so reject them.  Absolute
        # paths are unambiguous and supported.
        if ctx.scope is InstallScope.USER:
            local_path_str = dep_ref.local_path or ""
            if not local_path_str or not Path(local_path_str).expanduser().is_absolute():
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
                return None

        # Determine the anchor for relative ``local_path`` (#857). For
        # direct deps from the root project this is ``ctx.source_root``
        # (which equals ``ctx.project_root`` unless ``apm install --root``
        # redirects writes -- then it stays at $PWD).  For transitive
        # deps declared inside another local package, the parent's
        # source directory was captured during resolve via
        # ``ctx.dep_base_dirs`` -- it is already absolute, so ``--root``
        # has nothing to do.
        base_dir = getattr(ctx, "dep_base_dirs", {}).get(dep_key) or ctx.source_root
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
        depth, resolved_by, _is_dev = self._lockfile_node_fields()
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


class CachedDependencySource(DependencySource):
    """Cached dependency: already extracted under ``apm_modules/``."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from cached package"

    def __init__(
        self,
        ctx: InstallContext,
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
        resolved_ref: Any,
        dep_locked_chk: Any,
        fetched_this_run: bool = False,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        self.resolved_ref = resolved_ref
        self.dep_locked_chk = dep_locked_chk
        # F2 (#1116): when the resolver callback fetched this package
        # earlier in the SAME install run, we still hit the cached
        # source path (skip_download=True), but the install line should
        # NOT say "(cached)" -- bytes were just downloaded. The integrate
        # phase passes True here when the dep_key is in
        # ctx.callback_downloaded.
        self.fetched_this_run = fetched_this_run

    def _resolve_cached_commit(self) -> str | None:
        """Determine the SHA to record in the lockfile for the cached path.

        Invariant: when ``skip_download=True``, the SHA we record MUST
        equal what is actually on disk. The previous logic promoted
        ``resolved_ref.resolved_commit`` to the top of the priority list,
        which silently wrote the remote HEAD even when bytes had not been
        re-materialized -- producing a phantom identity in the lockfile
        (3-way drift bug, PR #1158).

        Priority:
        * ``fetched_this_run``: bytes were just downloaded by the
          resolver callback. Use the SHA captured at fetch time
          (callback) or the resolver's own SHA. Both reflect what
          landed on disk in this run. By construction the upstream
          download path always populates one of those two for a
          freshly-fetched dep, so we never fall back to the lockfile
          here -- doing so would risk overwriting on-disk bytes with a
          stale lockfile SHA.
        * true cached path: trust the existing lockfile SHA. It was
          written by a previous successful install and matches what is
          on disk (verified upstream by the lockfile_match check).
          NEVER use ``resolved_ref`` here.
        * fallback to ``dep_ref.reference`` only when no lockfile SHA
          is available (cold-path with no prior install) or when the
          fetched-this-run path failed to capture a SHA at all.
        """
        ctx = self.ctx
        dep_key = self.dep_key
        resolved_ref = self.resolved_ref
        dep_ref = self.dep_ref

        cached_commit: str | None = None
        if self.fetched_this_run:
            cached_commit = ctx.callback_downloaded.get(dep_key)
            if (
                not cached_commit
                and resolved_ref
                and resolved_ref.resolved_commit
                and resolved_ref.resolved_commit != "cached"
            ):
                cached_commit = resolved_ref.resolved_commit
        elif ctx.existing_lockfile:
            locked_dep = ctx.existing_lockfile.get_dependency(dep_key)
            if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
                cached_commit = locked_dep.resolved_commit
        if not cached_commit:
            cached_commit = dep_ref.reference
        return cached_commit

    def acquire(self) -> Materialization | None:
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
        # F3 (#1116): centralised hex/sentinel-aware short SHA helper.
        # Prefer the lockfile-recorded SHA when present; otherwise fall
        # back to the SHA captured by the parallel resolver callback in
        # this same install run (cold-path case where no lockfile exists
        # yet, but the resolver already learned the resolved commit).
        _sha = format_short_sha(dep_locked_chk.resolved_commit) if dep_locked_chk else ""
        if not _sha:
            _callback_sha = ctx.callback_downloaded.get(dep_key)
            if _callback_sha:
                _sha = format_short_sha(_callback_sha)
        if logger:
            logger.download_complete(
                display_name, ref=_ref, sha=_sha, cached=not self.fetched_this_run
            )

        deltas: dict[str, int] = {"installed": int(self.fetched_this_run)}
        if not dep_ref.reference:
            deltas["unpinned"] = 1

        # Skip integration entirely if no targets.  The template will
        # write the empty deployed_files entry on its own (single source
        # of truth), so we just signal "skip integration" via
        # package_info=None.
        # In lockfile_only mode, skip this early return so installed_packages
        # is populated before we return without deploying any files.
        if not ctx.targets and not ctx.lockfile_only:
            return self._skip_integration(deltas)

        # Load package from apm.yml. Anchor source_path on the clone location
        # so transitive ``local_path`` deps inside this remote package resolve
        # from there (#857).
        apm_yml_path = install_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            cached_package = APMPackage.from_apm_yml(apm_yml_path, source_path=install_path)
            # TODO(#940): see note in _materialize_local for the same caveat
            # about post-construction mutation of .source.
            if not cached_package.source:
                cached_package.source = dep_ref.repo_url
        else:
            cached_package = APMPackage(
                name=dep_ref.repo_url.split("/")[-1],
                version="unknown",
                package_path=install_path,
                source=dep_ref.repo_url,
            )

        resolved_or_cached_ref = (
            resolved_ref
            if resolved_ref
            else ResolvedReference(
                original_ref=dep_ref.reference or "default",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit="cached",
                ref_name=dep_ref.reference or "default",
            )
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
        depth, resolved_by, _is_dev = self._lockfile_node_fields()

        # Determine commit SHA for the cached path. See _resolve_cached_commit
        # for the invariant ("recorded SHA must match disk identity") and the
        # priority rules (PR #1158 -- branch-ref drift fix).
        cached_commit = self._resolve_cached_commit()

        # Determine if cached package came from registry
        _cached_registry = None
        if (dep_locked_chk and dep_locked_chk.registry_prefix) or (
            ctx.registry_config and not dep_ref.is_local
        ):
            _cached_registry = ctx.registry_config

        _cached_resolution = None
        if dep_ref.source == "registry":
            from apm_cli.deps.registry.feature_gate import (
                require_package_registry_enabled,
            )

            require_package_registry_enabled("Registry-sourced cached installs")
            _cached_resolution = registry_resolution_for_cached_registry_dep(
                ctx, dep_ref, dep_key, dep_locked_chk
            )

        # Cached git-source semver dep (#1488): replay the resolution from
        # either ctx (we resolved earlier in this same run) or the lockfile
        # so re-writing the lockfile from cache preserves constraint /
        # resolved_tag / resolved_at instead of dropping them. The
        # lockfile-backed reconstruction is gated on ALL required fields
        # being present (see ``_rebuild_cached_semver_resolution`` and the
        # PR #1496 review thread).
        _cached_semver = ctx.git_semver_resolutions.get(dep_key)
        if _cached_semver is None:
            _cached_semver = _rebuild_cached_semver_resolution(dep_locked_chk)

        ctx.installed_packages.append(
            InstalledPackage(
                dep_ref=dep_ref,
                resolved_commit=cached_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=_is_dev,
                registry_config=_cached_registry,
                registry_resolution=_cached_resolution,
                git_semver_resolution=_cached_semver,
            )
        )
        if install_path.is_dir():
            ctx.package_hashes[dep_key] = _compute_hash(install_path)
        if cached_package_info.package_type:
            ctx.package_types[dep_key] = cached_package_info.package_type.value

        # Return without deploying integration files when the target set is empty.
        if not ctx.targets:
            return self._skip_integration(deltas)

        return Materialization(
            package_info=cached_package_info,
            install_path=install_path,
            dep_key=dep_key,
            deltas=deltas,
        )


def make_dependency_source(
    ctx: InstallContext,
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
    *,
    resolved_ref: Any = None,
    dep_locked_chk: Any = None,
    ref_changed: bool = False,
    skip_download: bool = False,
    fetched_this_run: bool = False,
    progress: Any = None,
) -> DependencySource:
    """Factory: pick the right ``DependencySource`` for *dep_ref*.

    Caller is responsible for resolving the download strategy (cached vs
    fresh) before invoking the factory; the resolved-ref and
    locked-checksum data flow into the appropriate source.

    ``fetched_this_run`` (F2): when ``skip_download=True`` AND the
    package was actually downloaded earlier in this run by the resolver
    callback, set this to ``True`` so the cached source emits the
    download-complete line WITHOUT the misleading ``(cached)`` suffix.
    """
    if dep_ref.is_local and dep_ref.local_path:
        return LocalDependencySource(ctx, dep_ref, install_path, dep_key)
    if skip_download:
        return CachedDependencySource(
            ctx,
            dep_ref,
            install_path,
            dep_key,
            resolved_ref,
            dep_locked_chk,
            fetched_this_run=fetched_this_run,
        )
    return FreshDependencySource(
        ctx,
        dep_ref,
        install_path,
        dep_key,
        resolved_ref,
        dep_locked_chk,
        ref_changed,
        progress,
    )
