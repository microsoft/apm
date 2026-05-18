"""Cached dependency source -- package already extracted in ``apm_modules/``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apm_cli.install.sources._base import DependencySource, Materialization


@dataclass(frozen=True, slots=True)
class _CachedSourceExtras:
    resolved_ref: Any
    dep_locked_chk: Any
    fetched_this_run: bool = False


class CachedDependencySource(DependencySource):
    """Cached dependency: already extracted under ``apm_modules/``."""

    INTEGRATE_ERROR_PREFIX = "Failed to integrate primitives from cached package"

    def __init__(
        self,
        ctx: Any,
        dep_ref: Any,
        install_path: Any,
        dep_key: str,
        extras: _CachedSourceExtras | None = None,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        if extras is None:
            extras = _CachedSourceExtras(resolved_ref=None, dep_locked_chk=None)
        self.resolved_ref = extras.resolved_ref
        self.dep_locked_chk = extras.dep_locked_chk
        self.fetched_this_run = extras.fetched_this_run

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

    def _build_cached_package_info(self):
        from apm_cli.constants import APM_YML_FILENAME
        from apm_cli.models.apm_package import (
            APMPackage,
            GitReferenceType,
            PackageInfo,
            ResolvedReference,
        )
        from apm_cli.models.validation import detect_package_type

        dep_ref = self.dep_ref
        install_path = self.install_path
        resolved_ref = self.resolved_ref

        apm_yml_path = install_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            cached_package = APMPackage.from_apm_yml(apm_yml_path, source_path=install_path)
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
        return cached_package_info

    def acquire(self) -> Materialization | None:
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

        ctx = self.ctx
        dep_ref = self.dep_ref
        install_path = self.install_path
        dep_key = self.dep_key
        dep_locked_chk = self.dep_locked_chk
        logger = ctx.logger

        from apm_cli.utils.short_sha import format_short_sha

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

        deltas: dict[str, int] = {"installed": 1}
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

        cached_package_info = self._build_cached_package_info()

        # Collect for lockfile
        node = ctx.dependency_graph.dependency_tree.get_node(dep_key)
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False

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

        ctx.installed_packages.append(
            InstalledPackage(
                dep_ref=dep_ref,
                resolved_commit=cached_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=_is_dev,
                registry_config=_cached_registry,
            )
        )
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
