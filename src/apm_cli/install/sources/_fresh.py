"""Fresh (network download) dependency source.

Performs the download, supply-chain hash verification, lockfile
bookkeeping, and progress/TUI integration for dependencies that are not
cached on disk.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from apm_cli.install.sources._base import (
    DependencySource,
    Materialization,
    _format_package_type_label,
)
from apm_cli.utils.console import _rich_error, _rich_success
from apm_cli.utils.short_sha import format_short_sha


@dataclass(frozen=True, slots=True)
class _FreshSourceExtras:
    resolved_ref: Any
    dep_locked_chk: Any
    ref_changed: bool
    progress: Any = None


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
        ctx: Any,
        dep_ref: Any,
        install_path: Any,
        dep_key: str,
        extras: _FreshSourceExtras,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        self.resolved_ref = extras.resolved_ref
        self.dep_locked_chk = extras.dep_locked_chk
        self.ref_changed = extras.ref_changed
        self.progress = extras.progress

    # ------------------------------------------------------------------
    # Private helpers -- each encapsulates one logical concern so that
    # acquire() stays within complexity/branch/statement limits.
    # ------------------------------------------------------------------

    def _start_progress_tasks(self, short_name: str) -> Any:
        """Start progress-bar task and TUI task; return task_id (or None)."""
        task_id = None
        if self.progress is not None:
            task_id = self.progress.add_task(
                description=f"Fetching {short_name}",
                total=None,
            )
        if self.ctx.tui is not None:
            self.ctx.tui.task_started(self.dep_key, f"fetch {short_name}")
        return task_id

    def _finish_progress_tasks(self, task_id: Any) -> None:
        """Hide progress-bar task and mark TUI task completed."""
        if self.progress is not None and task_id is not None:
            self.progress.update(task_id, visible=False)
            self.progress.refresh()
        if self.ctx.tui is not None:
            self.ctx.tui.task_completed(self.dep_key)

    def _log_download_result(self, display_name: str, resolved: Any) -> None:
        """Emit the download-complete log/success line for a fresh download."""
        ctx = self.ctx
        dep_ref = self.dep_ref
        logger = ctx.logger

        if logger:
            _ref = resolved.ref_name if resolved and resolved.ref_name else ""
            _sha = format_short_sha(resolved.resolved_commit) if resolved else ""
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
                _s = format_short_sha(resolved.resolved_commit)
                if _r and _s:
                    _ref_suffix = f" #{_r} @{_s}"
                elif _r:
                    _ref_suffix = f" #{_r}"
                elif _s:
                    _ref_suffix = f" @{_s}"
            _rich_success(f"[+] {display_name}{_ref_suffix}")

    def _record_lockfile_entry(self, dep_ref: Any, resolved: Any, package_info: Any) -> None:
        """Append an InstalledPackage record and store the content hash."""
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

        ctx = self.ctx
        dep_key = self.dep_key
        install_path = self.install_path

        resolved_commit = resolved.resolved_commit if resolved else None
        node = ctx.dependency_graph.dependency_tree.get_node(dep_key)
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False
        ctx.installed_packages.append(
            InstalledPackage(
                dep_ref=dep_ref,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=_is_dev,
                registry_config=ctx.registry_config if not dep_ref.is_local else None,
            )
        )
        if install_path.is_dir():
            ctx.package_hashes[dep_key] = _compute_hash(install_path)

    def _verify_content_hash(self, dep_locked_chk: Any) -> None:
        """Abort via ``sys.exit(1)`` if the downloaded hash mismatches the lockfile.

        Skip when ``ctx.expected_hash_change_deps`` marks this dep (set by
        _resolve_download_strategy when branch-ref drift or the v<=0.12.2
        self-heal forces a re-download whose hash is legitimately expected to
        differ from the lockfile record).
        """
        from apm_cli.utils.path_security import safe_rmtree

        ctx = self.ctx
        dep_key = self.dep_key
        install_path = self.install_path

        if (
            not ctx.update_refs
            and dep_key not in ctx.expected_hash_change_deps
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

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def acquire(self) -> Materialization | None:
        from apm_cli.drift import build_download_ref

        ctx = self.ctx
        dep_ref = self.dep_ref
        dep_key = self.dep_key
        dep_locked_chk = self.dep_locked_chk
        diagnostics = ctx.diagnostics

        try:
            display_name = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
            short_name = display_name.split("/")[-1] if "/" in display_name else display_name

            # Workstream B (#1116): per-dep progress is owned by the
            # shared InstallTui ``ctx.tui``; legacy local Progress is
            # only wired when integrate is invoked outside the install
            # pipeline (no callers do this today, but the parameter is
            # kept for back-compat).
            task_id = self._start_progress_tasks(short_name)

            download_ref = build_download_ref(
                dep_ref,
                ctx.existing_lockfile,
                update_refs=ctx.update_refs,
                ref_changed=self.ref_changed,
            )

            if dep_key in ctx.pre_download_results:
                package_info = ctx.pre_download_results[dep_key]
            else:
                package_info = ctx.downloader.download_package(
                    download_ref,
                    self.install_path,
                    progress_task_id=task_id,
                    progress_obj=self.progress,
                )

            # CRITICAL: hide progress BEFORE printing success to avoid overlap
            self._finish_progress_tasks(task_id)

            deltas: dict[str, int] = {"installed": 1}
            resolved = getattr(package_info, "resolved_reference", None)
            self._log_download_result(display_name, resolved)

            if not dep_ref.reference:
                deltas["unpinned"] = 1

            # Lockfile bookkeeping + content hash recording
            self._record_lockfile_entry(dep_ref, resolved, package_info)

            # Supply-chain protection: verify hash on fresh downloads.
            self._verify_content_hash(dep_locked_chk)

            if hasattr(package_info, "package_type"):
                package_type = package_info.package_type
                if package_type:
                    ctx.package_types[dep_key] = package_type.value
                _type_label = _format_package_type_label(package_type)
                if _type_label and ctx.logger:
                    ctx.logger.package_type_info(_type_label)

            # If no targets, skip integration but keep deltas
            if not ctx.targets:
                return Materialization(
                    package_info=None,
                    install_path=self.install_path,
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
            try:  # noqa: SIM105
                self.progress.remove_task(task_id)  # type: ignore[name-defined]
            except Exception:
                pass
            diagnostics.error(
                f"Failed to install {display_name}: {e}",
                package=dep_key,
            )
            return None
