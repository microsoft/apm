"""Transitive download callback for the resolve phase.

Extracts the former ``download_callback`` closure out of
``resolve._resolve_dependencies`` into a stateful callable so the enclosing
phase function stays within the complexity / statement budget.  One instance
is created per resolve run; the resolver (``APMDependencyResolver``) invokes
it once per BFS edge -- possibly across a worker pool, hence the lock.

The instance accumulates ``downloaded`` / ``failures`` / ``transitive_failures``
which the resolve phase folds back onto the :class:`InstallContext` after
resolution completes (same mutable objects, read back by identity).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.drift import build_download_ref, detect_ref_change

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


class _TransitiveDownloader:
    """Callable that downloads one package during BFS dependency resolution.

    Replaces the former nested ``download_callback`` closure.  The
    ``__call__`` signature deliberately keeps ``parent_pkg`` so the resolver's
    ``_signature_accepts_parent_pkg`` introspection (which inspects
    ``__call__`` and skips ``self``) still routes the declaring-parent anchor
    through for transitive local deps (#857).

    The registry branch is kept inline in :meth:`__call__` (rather than
    extracted) so the ``dep_ref`` reassignment from
    ``_apply_lockfile_registry_name`` stays visible to the shared exception
    handler, preserving the exact failure-key behaviour of the original
    closure.
    """

    def __init__(
        self,
        ctx: InstallContext,
        *,
        registry_resolver,
        apply_lockfile_registry_name,
        registries_map,
        direct_dep_keys,
        update_refs: bool,
    ):
        self.ctx = ctx
        self.registry_resolver = registry_resolver
        self._apply_lockfile_registry_name = apply_lockfile_registry_name
        self.registries_map = registries_map
        self.direct_dep_keys = direct_dep_keys
        self.update_refs = update_refs
        # Snapshot the same ctx fields the former closure captured as locals.
        self.scope = ctx.scope
        self.project_root = ctx.project_root
        self.source_root = ctx.source_root
        self.logger = ctx.logger
        self.existing_lockfile = ctx.existing_lockfile
        self.downloader = ctx.downloader
        # Accumulators that escape back to ctx after resolution. Mutated in
        # place; the resolve phase reads these same objects by identity.
        self.downloaded: dict = {}
        self.transitive_failures: list = []
        self.failures: set = set()
        self.lock = threading.Lock()

    # -- TUI helpers (collapse the repeated getattr/guard blocks) ----------
    # ``ctx.tui`` is read live each call (never cached) to mirror the
    # original closure, which re-fetched it at every use site.
    def _tui_completed(self, dep_key) -> None:
        tui = getattr(self.ctx, "tui", None)
        if tui is not None:
            tui.task_completed(dep_key)

    def _tui_failed(self, dep_key) -> None:
        tui = getattr(self.ctx, "tui", None)
        if tui is not None:
            tui.task_failed(dep_key)

    def _force_semver_resolve(self, dep_ref) -> bool:
        """Pure predicate: should a cached install path fall through for a
        git-source semver dep under ``--update`` / ``--refresh``?"""
        return (
            self.update_refs
            and not dep_ref.is_local
            and getattr(dep_ref, "source", None) != "registry"
            and not getattr(dep_ref, "artifactory_prefix", None)
            and getattr(dep_ref, "ref_kind", None) == "semver"
        )

    def _emit_heartbeat(self, dep_ref) -> None:
        """Surface a heartbeat BEFORE the network/copy work so users see the
        install advancing past silent transitive lookups (#1116 F1/B)."""
        if not self.logger:
            return
        with self.lock:
            _display = dep_ref.get_display_name()
            _tui = getattr(self.ctx, "tui", None)
            if _tui is not None:
                _tui.task_started(dep_ref.get_unique_key(), f"resolve {_display}")
            if _tui is None or not _tui.is_animating():
                self.logger.resolving_heartbeat(_display)

    def __call__(self, dep_ref, modules_dir, parent_chain="", parent_pkg=None):
        """Download a package during dependency resolution.

        Args:
            dep_ref: The dependency to download.
            modules_dir: Target apm_modules directory.
            parent_chain: Human-readable breadcrumb (e.g. "root > mid")
                showing which dependency path led to this transitive dep.
            parent_pkg: APMPackage that declared *dep_ref*, or None for direct
                deps from the root project. For local deps we use its
                ``source_path`` as the anchor for relative paths so a
                transitive ``../sibling`` resolves against the declaring
                package's directory rather than the root consumer (#857).
        """
        install_path = dep_ref.get_install_path(modules_dir)
        # Cache short-circuit: skip the rest when the install path already
        # exists, unless this is a git-source semver dep under --update /
        # --refresh (then fall through so ``_maybe_resolve_git_semver``
        # re-runs ``git ls-remote`` and the lockfile gets rewritten with the
        # latest matching tag -- Bug 1 fix on #1496).
        if install_path.exists() and not self._force_semver_resolve(dep_ref):
            return install_path
        self._emit_heartbeat(dep_ref)
        try:
            # Registry-sourced dep (design 8): routed before local/git so the
            # registry resolver owns the download. Kept inline so the
            # ``dep_ref`` reassignment below is seen by the except handler.
            if dep_ref.source == "registry":
                return self._download_registry(dep_ref, install_path)
            # Local package: copy instead of git clone.
            if dep_ref.is_local and dep_ref.local_path:
                return self._download_local(dep_ref, install_path, parent_pkg)
            return self._download_git(dep_ref, install_path)
        except Exception as e:
            self._record_failure(dep_ref, e, parent_chain)
            return None

    def _download_registry(self, dep_ref, install_path):
        """Download a ``source == "registry"`` dep.

        Reassigns ``dep_ref`` via ``_apply_lockfile_registry_name`` exactly
        like the original closure; because ``get_unique_key()`` is invariant
        to ``registry_name`` the failure key is unchanged either way, but the
        rebind is preserved for parity.
        """
        from apm_cli.deps.registry.feature_gate import require_package_registry_enabled

        require_package_registry_enabled("Registry-sourced downloads")

        if self.registry_resolver is None:
            raise RuntimeError(
                f"dep {dep_ref.repo_url!r} is registry-sourced but no "
                f"registries: block is configured in apm.yml and the "
                f"lockfile carries no resolved_url for it."
            )
        dep_ref = self._apply_lockfile_registry_name(
            dep_ref,
            self.registries_map,
            existing_lockfile=self.existing_lockfile,
        )
        # Registry T5: honor lockfile on apm install. When the lockfile has
        # full replay data and the manifest range still covers the locked
        # version, fetch from the locked URL and verify against the locked
        # hash (npm install model -- no /versions API call).
        _locked_reg = (
            self.existing_lockfile.get_dependency(dep_ref.get_unique_key())
            if self.existing_lockfile
            else None
        )
        if (
            not self.update_refs
            and _locked_reg
            and _locked_reg.resolved_url
            and _locked_reg.resolved_hash
            and _locked_reg.version
        ):
            if not detect_ref_change(dep_ref, _locked_reg, update_refs=False):
                self.registry_resolver.download_from_lockfile(
                    dep_ref,
                    install_path,
                    resolved_url=_locked_reg.resolved_url,
                    resolved_hash=_locked_reg.resolved_hash,
                    version=_locked_reg.version,
                )
                self.downloaded[dep_ref.get_unique_key()] = None
                return install_path
        self.registry_resolver.download_package(dep_ref, install_path)
        # Mark as already-downloaded so the parallel pre-download phase skips
        # this dep. No SHA for registry deps.
        self.downloaded[dep_ref.get_unique_key()] = None
        return install_path

    def _download_local(self, dep_ref, install_path, parent_pkg):
        """Copy a local-path dep into the modules dir (no git clone)."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.local_content import _copy_local_package

        if (
            self.scope is InstallScope.USER
            and not Path(dep_ref.local_path).expanduser().is_absolute()
        ):
            # At user scope, relative local paths have no meaningful root
            # (cwd is arbitrary, $HOME is not a project). Reject them.
            with self.lock:
                self.failures.add(dep_ref.get_unique_key())
            self._tui_failed(dep_ref.get_unique_key())
            return None
        # Anchor relative paths on the declaring package's source directory
        # when available (#857); fall back to source_root for direct deps.
        base_dir = (
            parent_pkg.source_path
            if parent_pkg is not None and parent_pkg.source_path is not None
            else self.source_root
        )
        result_path = _copy_local_package(
            dep_ref,
            install_path,
            base_dir,
            project_root=self.project_root,
            logger=self.logger,
        )
        if result_path:
            with self.lock:
                self.downloaded[dep_ref.get_unique_key()] = None
            self._tui_completed(dep_ref.get_unique_key())
            return result_path
        self._tui_failed(dep_ref.get_unique_key())
        return None

    def _download_git(self, dep_ref, install_path):
        """Resolve any git-source semver range, detect spec drift, and clone."""
        from apm_cli.install.phases.resolve import _maybe_resolve_git_semver

        # Git-source semver range resolution (#1488): resolve a semver range
        # ``ref:`` to a concrete tag BEFORE any git operation. The result is
        # stashed on ctx so sources.py can plumb it into the lockfile; the
        # dep_ref's ``reference`` is rewritten in place to the concrete tag.
        _semver_resolution = _maybe_resolve_git_semver(
            dep_ref=dep_ref,
            existing_lockfile=self.existing_lockfile,
            update_refs=self.update_refs,
            auth_resolver=self.ctx.auth_resolver,
        )
        if _semver_resolution is not None:
            with self.lock:
                self.ctx.git_semver_resolutions[dep_ref.get_unique_key()] = _semver_resolution
            dep_ref.reference = _semver_resolution.resolved_tag

        # T5: use locked commit for reproducibility, unless the manifest ref
        # has drifted from what the lockfile recorded (spec drift).
        _locked_dep = (
            self.existing_lockfile.get_dependency(dep_ref.get_unique_key())
            if self.existing_lockfile
            else None
        )
        _ref_changed = detect_ref_change(dep_ref, _locked_dep, update_refs=self.update_refs)

        # When ref drifts, signal downstream that a content-hash change is
        # expected so the supply-chain check in sources.py doesn't treat a
        # legitimate re-resolution as an attack.
        if _ref_changed:
            with self.lock:
                self.ctx.expected_hash_change_deps.add(dep_ref.get_unique_key())
            if self.logger:
                _old = (
                    _locked_dep.resolved_ref or _locked_dep.resolved_commit[:8]
                    if _locked_dep
                    else "?"
                )
                _new = dep_ref.reference or "HEAD"
                self.logger.verbose_detail(
                    f"  [!] Spec drift: {dep_ref.get_unique_key()} {_old} -> {_new}, re-resolving"
                )

        download_dep = build_download_ref(
            dep_ref,
            self.existing_lockfile,
            update_refs=self.update_refs,
            ref_changed=_ref_changed,
        )

        # Silent download - no progress display for transitive deps.
        result = self.downloader.download_package(download_dep, install_path)
        # Capture resolved commit SHA for lockfile.
        resolved_sha = None
        if result and hasattr(result, "resolved_reference") and result.resolved_reference:
            resolved_sha = result.resolved_reference.resolved_commit
        with self.lock:
            self.downloaded[dep_ref.get_unique_key()] = resolved_sha
        self._tui_completed(dep_ref.get_unique_key())
        return install_path

    def _record_failure(self, dep_ref, e, parent_chain) -> None:
        """Record a download/resolution failure for deferred diagnostics."""
        # Distinguish resolution failures (git-semver no-match) from download
        # failures: the dep_ref was rewritten to a concrete tag BEFORE clone,
        # so a NoMatchingTagError means we never got to the download step.
        from apm_cli.deps.git_semver_resolver import NoMatchingTagError
        from apm_cli.models.dependency.reference import InvalidSemverRangeError

        dep_display = dep_ref.get_display_name()
        dep_key = dep_ref.get_unique_key()
        is_direct = dep_key in self.direct_dep_keys

        if isinstance(e, InvalidSemverRangeError):
            if is_direct:
                fail_msg = f"Invalid dependency spec for {dep_ref.repo_url}: {e}"
            else:
                chain_hint = f" (via {parent_chain})" if parent_chain else ""
                fail_msg = (
                    f"Invalid dependency spec for transitive dep "
                    f"{dep_ref.repo_url}{chain_hint}: {e}"
                )
        elif isinstance(e, NoMatchingTagError):
            if is_direct:
                fail_msg = f"No matching tag for {dep_ref.repo_url}: {e}"
            else:
                chain_hint = f" (via {parent_chain})" if parent_chain else ""
                fail_msg = f"No matching tag for transitive dep {dep_ref.repo_url}{chain_hint}: {e}"
        # Distinguish direct vs transitive failure messages so users don't see
        # a misleading "transitive dep" label for top-level deps.
        elif is_direct:
            fail_msg = f"Failed to download dependency {dep_ref.repo_url}: {e}"
        else:
            chain_hint = f" (via {parent_chain})" if parent_chain else ""
            fail_msg = f"Failed to resolve transitive dep {dep_ref.repo_url}{chain_hint}: {e}"

        # F7 (#1116): single critical section for both the logger emission and
        # the result-recording so concurrent failures don't interleave.
        with self.lock:
            if self.logger:
                self.logger.verbose_detail(f"  {fail_msg}")
            self.failures.add(dep_key)
            self.transitive_failures.append((dep_display, fail_msg))
        self._tui_failed(dep_key)
