"""Fresh-download dependency source for the install pipeline.

Split out of ``apm_cli.install.sources`` to keep that module under the
file-length budget.  ``FreshDependencySource`` is the network path: it
downloads a dependency that is not already cached, runs supply-chain hash
verification against the lockfile, and records the install for lockfile
write-back.

The public class is re-exported from ``apm_cli.install.sources`` so existing
``from apm_cli.install.sources import FreshDependencySource`` imports keep
working.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from apm_cli.install.registry_wiring import (
    get_registry_resolver,
    resolver_last_registry_resolution,
)
from apm_cli.install.sources_base import DependencySource, Materialization
from apm_cli.utils.console import _rich_error, _rich_success
from apm_cli.utils.short_sha import format_short_sha

if TYPE_CHECKING:
    from pathlib import Path

    from apm_cli.install.context import InstallContext


def _format_package_type_label(pkg_type) -> str | None:
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
        PackageType.MARKETPLACE_PLUGIN: "Marketplace Plugin (plugin.json or agents/skills/commands)",
        PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
        PackageType.APM_PACKAGE: "APM Package (apm.yml)",
        PackageType.HOOK_PACKAGE: "Hook Package (hooks/*.json only)",
        PackageType.SKILL_BUNDLE: "Skill Bundle (skills/<name>/SKILL.md)",
    }.get(pkg_type)


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
        ctx: InstallContext,
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
        resolved_ref: Any,
        dep_locked_chk: Any,
        ref_changed: bool,
        progress: Any = None,
    ):
        super().__init__(ctx, dep_ref, install_path, dep_key)
        self.resolved_ref = resolved_ref
        self.dep_locked_chk = dep_locked_chk
        self.ref_changed = ref_changed
        self.progress = progress

    def acquire(self) -> Materialization | None:
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.drift import build_download_ref
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
            short_name = display_name.split("/")[-1] if "/" in display_name else display_name

            # Workstream B (#1116): per-dep progress is owned by the
            # shared InstallTui ``ctx.tui``; legacy local Progress is
            # only wired when integrate is invoked outside the install
            # pipeline (no callers do this today, but the parameter is
            # kept for back-compat).
            task_id = None
            if progress is not None:
                task_id = progress.add_task(
                    description=f"Fetching {short_name}",
                    total=None,
                )
            if ctx.tui is not None:
                ctx.tui.task_started(dep_key, f"fetch {short_name}")

            download_ref = build_download_ref(
                dep_ref,
                ctx.existing_lockfile,
                update_refs=ctx.update_refs,
                ref_changed=ref_changed,
            )

            if dep_key in ctx.pre_download_results:
                package_info = ctx.pre_download_results[dep_key]
            elif dep_ref.source == "registry":
                from apm_cli.deps.registry.feature_gate import (
                    require_package_registry_enabled,
                )

                require_package_registry_enabled("Registry-sourced downloads")

                # Registry-sourced dep: dispatch to the dedicated-registry
                # resolver instead of the GitHub downloader. This branch
                # fires when (a) the BFS callback skipped due to existing
                # install path on a re-install, or (b) parallel pre-download
                # was skipped (registry deps aren't pre-downloaded).
                _registry_resolver = get_registry_resolver(ctx)
                if _registry_resolver is None:
                    raise RuntimeError(
                        f"dep {dep_ref.repo_url!r} is registry-sourced but "
                        f"no registry resolver was constructed (apm.yml may "
                        f"be missing a 'registries:' block)."
                    )
                # Lockfile re-install path: registry_name might be absent --
                # look it up from the lockfile's resolved_url.
                from apm_cli.deps.registry.auth import (
                    dependency_ref_with_registry_name_from_lockfile,
                )

                _regs = getattr(ctx.apm_package, "registries", None) or {}
                download_ref = dependency_ref_with_registry_name_from_lockfile(
                    download_ref,
                    _regs,
                    locked_dep=dep_locked_chk,
                )
                # Lockfile replay (npm install model): fetch directly from the
                # locked URL and verify against the locked hash when available
                # and the manifest range still covers the locked version.
                if (
                    not ctx.update_refs
                    and dep_locked_chk
                    and dep_locked_chk.resolved_url
                    and dep_locked_chk.resolved_hash
                    and dep_locked_chk.version
                    and not ref_changed
                ):
                    package_info = _registry_resolver.download_from_lockfile(
                        download_ref,
                        install_path,
                        resolved_url=dep_locked_chk.resolved_url,
                        resolved_hash=dep_locked_chk.resolved_hash,
                        version=dep_locked_chk.version,
                    )
                else:
                    package_info = _registry_resolver.download_package(
                        download_ref,
                        install_path,
                    )
            else:
                package_info = ctx.downloader.download_package(
                    download_ref,
                    install_path,
                    progress_task_id=task_id,
                    progress_obj=progress,
                )

            # CRITICAL: hide progress BEFORE printing success to avoid overlap
            if progress is not None and task_id is not None:
                progress.update(task_id, visible=False)
                progress.refresh()
            if ctx.tui is not None:
                ctx.tui.task_completed(dep_key)

            deltas: dict[str, int] = {"installed": 1}

            resolved = getattr(package_info, "resolved_reference", None)
            if logger:
                _ref = ""
                _sha = ""
                if resolved:
                    _ref = resolved.ref_name if resolved.ref_name else ""
                    # F3 (#1116): centralised hex/sentinel-aware short SHA helper.
                    _sha = format_short_sha(resolved.resolved_commit)
                logger.download_complete(display_name, ref=_ref, sha=_sha)
                # Only emit the per-package git auth diagnostic for git deps.
                # Registry-sourced deps don't talk to git hosts; resolving
                # github.com auth here for them is misleading (and can issue
                # network calls via auth.AuthResolver providers).
                if ctx.auth_resolver and dep_ref.source in (None, "git"):
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

            if not dep_ref.reference:
                deltas["unpinned"] = 1

            # Lockfile bookkeeping
            resolved_commit = None
            if resolved:
                resolved_commit = package_info.resolved_reference.resolved_commit
            depth, resolved_by, _is_dev = self._lockfile_node_fields()
            # Registry-sourced deps: pull the captured resolution out of
            # the resolver's per-graph map so the lockfile records
            # resolved_url + resolved_hash + version (design 6.1).
            _registry_resolution = (
                resolver_last_registry_resolution(ctx, dep_key)
                if dep_ref.source == "registry"
                else None
            )
            # Git-source semver-range deps (#1488): the resolution was
            # captured by the BFS download_callback in phases/resolve.py.
            _git_semver_resolution = ctx.git_semver_resolutions.get(dep_key)
            ctx.installed_packages.append(
                InstalledPackage(
                    dep_ref=dep_ref,
                    resolved_commit=resolved_commit,
                    depth=depth,
                    resolved_by=resolved_by,
                    is_dev=_is_dev,
                    registry_config=(ctx.registry_config if not dep_ref.is_local else None),
                    registry_resolution=_registry_resolution,
                    git_semver_resolution=_git_semver_resolution,
                )
            )
            if install_path.is_dir():
                ctx.package_hashes[dep_key] = _compute_hash(install_path)

            # Supply-chain protection: verify content hash on fresh
            # downloads when the lockfile already records a hash.
            # Skip when ``ctx.expected_hash_change_deps`` marks this dep
            # (set by resolve.py's BFS callback and _resolve_download_strategy
            # when branch-ref drift or the v<=0.12.2 self-heal forces a
            # re-download whose hash is legitimately expected to differ from
            # the lockfile record).
            # Thread-safety: resolve phase completes before integrate runs,
            # so the set is stable here.  integrate.py's own .add() is
            # idempotent (set semantics) and runs single-threaded.
            _expected_hash_deps = ctx.expected_hash_change_deps
            if (
                not ctx.update_refs
                and dep_key not in _expected_hash_deps
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
                return self._skip_integration(deltas)

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
                progress.remove_task(task_id)  # type: ignore[name-defined]
            except Exception:
                pass
            diagnostics.error(
                f"Failed to install {display_name}: {e}",
                package=dep_key,
            )
            return None
