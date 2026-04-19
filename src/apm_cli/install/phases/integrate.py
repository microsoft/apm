"""Sequential integration phase -- per-package integration loop.

Reads all prior phase outputs from *ctx* (resolve, targets, download) and
processes each dependency sequentially: local-copy packages, cached packages,
and freshly-downloaded packages.  For every package the loop:

1. Builds a ``PackageInfo`` (or reuses the pre-downloaded result).
2. Runs the pre-deploy security scan.
3. Calls ``_integrate_package_primitives`` (via module-attribute access on
   ``apm_cli.commands.install`` so that test patches at
   ``@patch("apm_cli.commands.install._integrate_package_primitives")``
   continue to intercept the call).
4. Accumulates deployed-file lists, content hashes, and integration totals
   on *ctx* for the downstream cleanup and lockfile phases.

After the dependency loop, root-project primitives (``<project_root>/.apm/``)
are integrated when present (#714).

**Test-patch contract**: every name that tests patch at
``apm_cli.commands.install.X`` is accessed via the ``_install_mod.X``
indirection rather than a bare-name import.  This includes at minimum:
``_integrate_package_primitives``, ``_rich_success``, ``_rich_error``,
``_copy_local_package``, ``_pre_deploy_security_scan``.  All five private
helpers in this module (``_resolve_download_strategy``,
``_integrate_local_dep``, ``_integrate_cached_dep``,
``_integrate_fresh_dep``, ``_integrate_root_project``) honour this
contract via the ``_install_mod`` parameter.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


# ======================================================================
# Private helpers -- each encapsulates one per-package integration path
# ======================================================================


def _resolve_download_strategy(
    ctx: "InstallContext",
    dep_ref: Any,
    install_path: Path,
) -> Tuple[Any, bool, Any, bool]:
    """Determine whether *dep_ref* can be served from cache.

    Returns ``(resolved_ref, skip_download, dep_locked_chk, ref_changed)``
    where *skip_download* is ``True`` when the package at *install_path*
    is already up-to-date.
    """
    from apm_cli.models.apm_package import GitReferenceType
    from apm_cli.drift import detect_ref_change
    from apm_cli.utils.path_security import safe_rmtree

    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # npm-like behavior: Branches always fetch latest, only tags/commits use cache
    # Resolve git reference to determine type
    resolved_ref = None
    if dep_ref.get_unique_key() not in ctx.pre_downloaded_keys:
        # Resolve when there is an explicit ref, OR when update_refs
        # is True AND we have a non-cached lockfile entry to compare
        # against (otherwise resolution is wasted work -- the package
        # will be downloaded regardless).
        _has_lockfile_sha = False
        if update_refs and existing_lockfile:
            _lck = existing_lockfile.get_dependency(dep_ref.get_unique_key())
            _has_lockfile_sha = bool(
                _lck and _lck.resolved_commit and _lck.resolved_commit != "cached"
            )
        if dep_ref.reference or (update_refs and _has_lockfile_sha):
            try:
                resolved_ref = ctx.downloader.resolve_git_reference(dep_ref)
            except Exception:
                pass  # If resolution fails, skip cache (fetch latest)

    # Use cache only for tags and commits (not branches)
    is_cacheable = resolved_ref and resolved_ref.ref_type in [
        GitReferenceType.TAG,
        GitReferenceType.COMMIT,
    ]
    # Skip download if: already fetched by resolver callback, or cached tag/commit
    already_resolved = dep_ref.get_unique_key() in ctx.callback_downloaded
    # Detect if manifest ref changed vs what the lockfile recorded.
    # detect_ref_change() handles all transitions including None->ref.
    _dep_locked_chk = (
        existing_lockfile.get_dependency(dep_ref.get_unique_key())
        if existing_lockfile
        else None
    )
    ref_changed = detect_ref_change(
        dep_ref, _dep_locked_chk, update_refs=update_refs
    )
    # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
    # -- but not when the manifest ref has changed (user wants different version).
    lockfile_match = False
    if install_path.exists() and existing_lockfile:
        locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
        if locked_dep and locked_dep.resolved_commit and locked_dep.resolved_commit != "cached":
            if update_refs:
                # Update mode: compare resolved remote SHA with lockfile SHA.
                # If the remote ref still resolves to the same commit,
                # the package content is unchanged -- skip download.
                # Also verify local checkout matches to guard against
                # corrupted installs that bypassed pre-download checks.
                if resolved_ref and resolved_ref.resolved_commit == locked_dep.resolved_commit:
                    try:
                        from git import Repo as GitRepo
                        local_repo = GitRepo(install_path)
                        if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                            lockfile_match = True
                    except Exception:
                        pass  # Local checkout invalid -- fall through to download
            elif not ref_changed:
                # Normal mode: compare local HEAD with lockfile SHA.
                try:
                    from git import Repo as GitRepo
                    local_repo = GitRepo(install_path)
                    if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                        lockfile_match = True
                except Exception:
                    pass  # Not a git repo or invalid -- fall through to download
    skip_download = install_path.exists() and (
        (is_cacheable and not update_refs)
        or (already_resolved and not update_refs)
        or lockfile_match
    )

    # Verify content integrity when lockfile has a hash
    if skip_download and _dep_locked_chk and _dep_locked_chk.content_hash:
        from apm_cli.utils.content_hash import verify_package_hash
        if not verify_package_hash(install_path, _dep_locked_chk.content_hash):
            _hash_msg = (
                f"Content hash mismatch for "
                f"{dep_ref.get_unique_key()} -- re-downloading"
            )
            diagnostics.warn(_hash_msg, package=dep_ref.get_unique_key())
            if logger:
                logger.progress(_hash_msg)
            safe_rmtree(install_path, ctx.apm_modules_dir)
            skip_download = False

    # When registry-only mode is active, bypass cache if the
    # cached artifact was NOT previously downloaded via the
    # registry (no registry_prefix in lockfile). This handles
    # the transition from direct-VCS installs to proxy installs
    # for packages not yet in the lockfile.
    if (
        skip_download
        and ctx.registry_config
        and ctx.registry_config.enforce_only
        and not dep_ref.is_local
    ):
        if not _dep_locked_chk or _dep_locked_chk.registry_prefix is None:
            skip_download = False

    return resolved_ref, skip_download, _dep_locked_chk, ref_changed


def _integrate_local_dep(
    ctx: "InstallContext",
    _install_mod: Any,
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
) -> Optional[Dict[str, int]]:
    """Integrate a local (filesystem) package.

    Returns a counter-delta dict, or ``None`` if the dependency was
    skipped (user scope, copy failure).
    """
    from apm_cli.core.scope import InstallScope
    from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # User scope: relative paths would resolve against $HOME
    # instead of cwd, producing wrong results.  Skip with a
    # clear diagnostic rather than silently failing.
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

    result_path = _install_mod._copy_local_package(dep_ref, install_path, ctx.project_root, logger=logger)
    if not result_path:
        diagnostics.error(
            f"Failed to copy local package: {dep_ref.local_path}",
            package=dep_ref.local_path,
        )
        return None

    deltas: Dict[str, int] = {"installed": 1}
    if logger:
        logger.download_complete(dep_ref.local_path, ref_suffix="local")

    # Build minimal PackageInfo for integration
    from apm_cli.models.apm_package import (
        APMPackage,
        PackageInfo,
        PackageType,
        ResolvedReference,
        GitReferenceType,
    )
    from datetime import datetime

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
    from apm_cli.models.validation import detect_package_type
    pkg_type, plugin_json_path = detect_package_type(install_path)
    local_info.package_type = pkg_type
    if pkg_type == PackageType.MARKETPLACE_PLUGIN:
        # Normalize: synthesize .apm/ from plugin.json so
        # integration can discover and deploy primitives
        from apm_cli.deps.plugin_parser import normalize_plugin_directory
        normalize_plugin_directory(install_path, plugin_json_path)

    # Record for lockfile
    from apm_cli.deps.installed_package import InstalledPackage
    node = ctx.dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
    depth = node.depth if node else 1
    resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
    _is_dev = node.is_dev if node else False
    ctx.installed_packages.append(InstalledPackage(
        dep_ref=dep_ref, resolved_commit=None,
        depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
        registry_config=None,  # local deps never go through registry
    ))
    dep_key = dep_ref.get_unique_key()
    if install_path.is_dir() and not dep_ref.is_local:
        ctx.package_hashes[dep_key] = _compute_hash(install_path)
    dep_deployed_files: builtins.list = []

    if hasattr(local_info, 'package_type') and local_info.package_type:
        ctx.package_types[dep_key] = local_info.package_type.value

    # Use the same variable name as the rest of the loop
    package_info = local_info

    # Run shared integration pipeline
    try:
        # Pre-deploy security gate
        if not _install_mod._pre_deploy_security_scan(
            install_path, diagnostics,
            package_name=dep_key, force=ctx.force,
            logger=logger,
        ):
            ctx.package_deployed_files[dep_key] = []
            return deltas

        int_result = _install_mod._integrate_package_primitives(
            package_info, ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=ctx.managed_files,
            diagnostics=diagnostics,
            package_name=dep_key,
            logger=logger,
            scope=ctx.scope,
        )
        deltas["prompts"] = int_result["prompts"]
        deltas["agents"] = int_result["agents"]
        deltas["skills"] = int_result["skills"]
        deltas["sub_skills"] = int_result["sub_skills"]
        deltas["instructions"] = int_result["instructions"]
        deltas["commands"] = int_result["commands"]
        deltas["hooks"] = int_result["hooks"]
        deltas["links_resolved"] = int_result["links_resolved"]
        dep_deployed_files.extend(int_result["deployed_files"])
    except Exception as e:
        diagnostics.error(
            f"Failed to integrate primitives from local package: {e}",
            package=dep_ref.local_path,
        )

    ctx.package_deployed_files[dep_key] = dep_deployed_files

    # In verbose mode, show inline skip/error count for this package
    if logger and logger.verbose:
        _skip_count = diagnostics.count_for_package(dep_key, "collision")
        _err_count = diagnostics.count_for_package(dep_key, "error")
        if _skip_count > 0:
            noun = "file" if _skip_count == 1 else "files"
            logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
        if _err_count > 0:
            noun = "error" if _err_count == 1 else "errors"
            logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

    return deltas


def _integrate_cached_dep(
    ctx: "InstallContext",
    _install_mod: Any,
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
    resolved_ref: Any,
    dep_locked_chk: Any,
) -> Optional[Dict[str, int]]:
    """Integrate a cached (already-downloaded) package.

    Returns a counter-delta dict.
    """
    from apm_cli.constants import APM_YML_FILENAME
    from apm_cli.utils.content_hash import compute_package_hash as _compute_hash

    logger = ctx.logger
    diagnostics = ctx.diagnostics

    display_name = (
        str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
    )
    # Show resolved ref from lockfile for consistency with fresh installs
    _ref = dep_ref.reference or ""
    _sha = ""
    if dep_locked_chk and dep_locked_chk.resolved_commit and dep_locked_chk.resolved_commit != "cached":
        _sha = dep_locked_chk.resolved_commit[:8]
    if logger:
        logger.download_complete(display_name, ref=_ref, sha=_sha, cached=True)

    deltas: Dict[str, int] = {"installed": 1}
    if not dep_ref.reference:
        deltas["unpinned"] = 1

    # Skip integration if not needed
    if not ctx.targets:
        return deltas

    # Integrate prompts for cached packages (zero-config behavior)
    try:
        # Create PackageInfo from cached package
        from apm_cli.models.apm_package import (
            APMPackage,
            PackageInfo,
            PackageType,
            ResolvedReference,
            GitReferenceType,
        )
        from datetime import datetime

        # Load package from apm.yml in install path
        apm_yml_path = install_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            cached_package = APMPackage.from_apm_yml(apm_yml_path)
            # Ensure source is set to the repo URL for sync matching
            if not cached_package.source:
                cached_package.source = dep_ref.repo_url
        else:
            # Virtual package or no apm.yml - create minimal package
            cached_package = APMPackage(
                name=dep_ref.repo_url.split("/")[-1],
                version="unknown",
                package_path=install_path,
                source=dep_ref.repo_url,
            )

        # Use resolved reference from ref resolution if available
        # (e.g. when update_refs matched the lockfile SHA),
        # otherwise create a placeholder for cached packages.
        resolved_or_cached_ref = resolved_ref if resolved_ref else ResolvedReference(
            original_ref=dep_ref.reference or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="cached",  # Mark as cached since we don't know exact commit
            ref_name=dep_ref.reference or "default",
        )

        cached_package_info = PackageInfo(
            package=cached_package,
            install_path=install_path,
            resolved_reference=resolved_or_cached_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,  # Store for canonical dependency string
        )

        # Detect package_type from disk contents so
        # skill integration is not silently skipped
        from apm_cli.models.validation import detect_package_type
        pkg_type, _ = detect_package_type(install_path)
        cached_package_info.package_type = pkg_type

        # Collect for lockfile (cached packages still need to be tracked)
        from apm_cli.deps.installed_package import InstalledPackage
        node = ctx.dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False
        # Get commit SHA: resolved ref > callback capture > existing lockfile > explicit reference
        dep_key = dep_ref.get_unique_key()
        cached_commit = None
        if resolved_ref and resolved_ref.resolved_commit and resolved_ref.resolved_commit != "cached":
            cached_commit = resolved_ref.resolved_commit
        if not cached_commit:
            cached_commit = ctx.callback_downloaded.get(dep_key)
        if not cached_commit and ctx.existing_lockfile:
            locked_dep = ctx.existing_lockfile.get_dependency(dep_key)
            if locked_dep:
                cached_commit = locked_dep.resolved_commit
        if not cached_commit:
            cached_commit = dep_ref.reference
        # Determine if the cached package came from the registry:
        # prefer the lockfile record, then the current registry config.
        _cached_registry = None
        if dep_locked_chk and dep_locked_chk.registry_prefix:
            # Reconstruct RegistryConfig from lockfile to preserve original source
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
        # Track package type for lockfile
        if hasattr(cached_package_info, 'package_type') and cached_package_info.package_type:
            ctx.package_types[dep_key] = cached_package_info.package_type.value

        # Pre-deploy security gate
        if not _install_mod._pre_deploy_security_scan(
            install_path, diagnostics,
            package_name=dep_key, force=ctx.force,
            logger=logger,
        ):
            ctx.package_deployed_files[dep_key] = []
            return deltas

        int_result = _install_mod._integrate_package_primitives(
            cached_package_info, ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=ctx.managed_files,
            diagnostics=diagnostics,
            package_name=dep_key,
            logger=logger,
            scope=ctx.scope,
        )
        deltas["prompts"] = int_result["prompts"]
        deltas["agents"] = int_result["agents"]
        deltas["skills"] = int_result["skills"]
        deltas["sub_skills"] = int_result["sub_skills"]
        deltas["instructions"] = int_result["instructions"]
        deltas["commands"] = int_result["commands"]
        deltas["hooks"] = int_result["hooks"]
        deltas["links_resolved"] = int_result["links_resolved"]
        dep_deployed = int_result["deployed_files"]
        ctx.package_deployed_files[dep_key] = dep_deployed
    except Exception as e:
        diagnostics.error(
            f"Failed to integrate primitives from cached package: {e}",
            package=dep_key,
        )

    # In verbose mode, show inline skip/error count for this package
    if logger and logger.verbose:
        _skip_count = diagnostics.count_for_package(dep_key, "collision")
        _err_count = diagnostics.count_for_package(dep_key, "error")
        if _skip_count > 0:
            noun = "file" if _skip_count == 1 else "files"
            logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
        if _err_count > 0:
            noun = "error" if _err_count == 1 else "errors"
            logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

    return deltas


def _integrate_fresh_dep(
    ctx: "InstallContext",
    _install_mod: Any,
    dep_ref: Any,
    install_path: Path,
    dep_key: str,
    resolved_ref: Any,
    dep_locked_chk: Any,
    ref_changed: bool,
    progress: Any,
) -> Optional[Dict[str, int]]:
    """Download and integrate a fresh (not cached) package.

    Returns a counter-delta dict, or ``None`` if the download failed.
    """
    from apm_cli.drift import build_download_ref
    from apm_cli.deps.installed_package import InstalledPackage
    from apm_cli.utils.content_hash import compute_package_hash as _compute_hash
    from apm_cli.utils.path_security import safe_rmtree

    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # Download the package with progress feedback
    try:
        display_name = (
            str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
        )
        short_name = (
            display_name.split("/")[-1]
            if "/" in display_name
            else display_name
        )

        # Create a progress task for this download
        task_id = progress.add_task(
            description=f"Fetching {short_name}",
            total=None,  # Indeterminate initially; git will update with actual counts
        )

        # T5: Build download ref - use locked commit if available.
        # build_download_ref() uses manifest ref when ref_changed is True.
        download_ref = build_download_ref(
            dep_ref, ctx.existing_lockfile, update_refs=ctx.update_refs, ref_changed=ref_changed
        )

        # Phase 4 (#171): Use pre-downloaded result if available
        _dep_key = dep_ref.get_unique_key()
        if _dep_key in ctx.pre_download_results:
            package_info = ctx.pre_download_results[_dep_key]
        else:
            # Fallback: sequential download (should rarely happen)
            package_info = ctx.downloader.download_package(
                download_ref,
                install_path,
                progress_task_id=task_id,
                progress_obj=progress,
            )

        # CRITICAL: Hide progress BEFORE printing success message to avoid overlap
        progress.update(task_id, visible=False)
        progress.refresh()  # Force immediate refresh to hide the bar

        deltas: Dict[str, int] = {"installed": 1}

        # Show resolved ref alongside package name for visibility
        resolved = getattr(package_info, 'resolved_reference', None)
        if logger:
            _ref = ""
            _sha = ""
            if resolved:
                _ref = resolved.ref_name if resolved.ref_name else ""
                _sha = resolved.resolved_commit[:8] if resolved.resolved_commit else ""
            logger.download_complete(display_name, ref=_ref, sha=_sha)
            # Log auth source for this download (verbose only)
            if ctx.auth_resolver:
                try:
                    _host = dep_ref.host or "github.com"
                    _org = dep_ref.repo_url.split('/')[0] if dep_ref.repo_url and '/' in dep_ref.repo_url else None
                    _ctx = ctx.auth_resolver.resolve(_host, org=_org)
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
            _install_mod._rich_success(f"[+] {display_name}{_ref_suffix}")

        # Track unpinned deps for aggregated diagnostic
        if not dep_ref.reference:
            deltas["unpinned"] = 1

        # Collect for lockfile: get resolved commit and depth
        resolved_commit = None
        if resolved:
            resolved_commit = package_info.resolved_reference.resolved_commit
        # Get depth from dependency tree
        node = ctx.dependency_graph.dependency_tree.get_node(dep_ref.get_unique_key())
        depth = node.depth if node else 1
        resolved_by = node.parent.dependency_ref.repo_url if node and node.parent else None
        _is_dev = node.is_dev if node else False
        ctx.installed_packages.append(InstalledPackage(
            dep_ref=dep_ref, resolved_commit=resolved_commit,
            depth=depth, resolved_by=resolved_by, is_dev=_is_dev,
            registry_config=ctx.registry_config if not dep_ref.is_local else None,
        ))
        if install_path.is_dir():
            ctx.package_hashes[dep_ref.get_unique_key()] = _compute_hash(install_path)

        # Supply chain protection: verify content hash on fresh
        # downloads when the lockfile already records a hash.
        # A mismatch means the downloaded content differs from
        # what was previously locked -- possible tampering.
        if (
            not ctx.update_refs
            and dep_locked_chk
            and dep_locked_chk.content_hash
            and dep_ref.get_unique_key() in ctx.package_hashes
        ):
            _fresh_hash = ctx.package_hashes[dep_ref.get_unique_key()]
            if _fresh_hash != dep_locked_chk.content_hash:
                safe_rmtree(install_path, ctx.apm_modules_dir)
                _install_mod._rich_error(
                    f"Content hash mismatch for "
                    f"{dep_ref.get_unique_key()}: "
                    f"expected {dep_locked_chk.content_hash}, "
                    f"got {_fresh_hash}. "
                    "The downloaded content differs from the "
                    "lockfile record. This may indicate a "
                    "supply-chain attack. Use 'apm install "
                    "--update' to accept new content and "
                    "update the lockfile."
                )
                sys.exit(1)

        # Track package type for lockfile
        if hasattr(package_info, 'package_type') and package_info.package_type:
            ctx.package_types[dep_ref.get_unique_key()] = package_info.package_type.value

        # Show package type in verbose mode
        if hasattr(package_info, "package_type"):
            from apm_cli.models.apm_package import PackageType

            package_type = package_info.package_type
            _type_label = {
                PackageType.CLAUDE_SKILL: "Skill (SKILL.md detected)",
                PackageType.MARKETPLACE_PLUGIN: "Marketplace Plugin (plugin.json detected)",
                PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
                PackageType.APM_PACKAGE: "APM Package (apm.yml)",
            }.get(package_type)
            if _type_label and logger:
                logger.package_type_info(_type_label)

        # Auto-integrate prompts and agents if enabled
        # Pre-deploy security gate
        if not _install_mod._pre_deploy_security_scan(
            package_info.install_path, diagnostics,
            package_name=dep_ref.get_unique_key(), force=ctx.force,
            logger=logger,
        ):
            ctx.package_deployed_files[dep_ref.get_unique_key()] = []
            return deltas

        if ctx.targets:
            try:
                int_result = _install_mod._integrate_package_primitives(
                    package_info, ctx.project_root,
                    targets=ctx.targets,
                    prompt_integrator=ctx.integrators["prompt"],
                    agent_integrator=ctx.integrators["agent"],
                    skill_integrator=ctx.integrators["skill"],
                    instruction_integrator=ctx.integrators["instruction"],
                    command_integrator=ctx.integrators["command"],
                    hook_integrator=ctx.integrators["hook"],
                    force=ctx.force,
                    managed_files=ctx.managed_files,
                    diagnostics=diagnostics,
                    package_name=dep_ref.get_unique_key(),
                    logger=logger,
                    scope=ctx.scope,
                )
                deltas["prompts"] = int_result["prompts"]
                deltas["agents"] = int_result["agents"]
                deltas["skills"] = int_result["skills"]
                deltas["sub_skills"] = int_result["sub_skills"]
                deltas["instructions"] = int_result["instructions"]
                deltas["commands"] = int_result["commands"]
                deltas["hooks"] = int_result["hooks"]
                deltas["links_resolved"] = int_result["links_resolved"]
                dep_deployed_fresh = int_result["deployed_files"]
                ctx.package_deployed_files[dep_ref.get_unique_key()] = dep_deployed_fresh
            except Exception as e:
                # Don't fail installation if integration fails
                diagnostics.error(
                    f"Failed to integrate primitives: {e}",
                    package=dep_ref.get_unique_key(),
                )

            # In verbose mode, show inline skip/error count for this package
            if logger and logger.verbose:
                pkg_key = dep_ref.get_unique_key()
                _skip_count = diagnostics.count_for_package(pkg_key, "collision")
                _err_count = diagnostics.count_for_package(pkg_key, "error")
                if _skip_count > 0:
                    noun = "file" if _skip_count == 1 else "files"
                    logger.package_inline_warning(f"    [!] {_skip_count} {noun} skipped (local files exist)")
                if _err_count > 0:
                    noun = "error" if _err_count == 1 else "errors"
                    logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

        return deltas

    except Exception as e:
        display_name = (
            str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
        )
        # Remove the progress task on error
        if "task_id" in locals():
            progress.remove_task(task_id)
        diagnostics.error(
            f"Failed to install {display_name}: {e}",
            package=dep_ref.get_unique_key(),
        )
        # Continue with other packages instead of failing completely
        return None


def _integrate_root_project(
    ctx: "InstallContext",
    _install_mod: Any,
) -> Optional[Dict[str, int]]:
    """Integrate root project's own .apm/ primitives (#714).

    Users should not need a dummy "./agent/apm.yml" stub to get their
    root-level .apm/ rules deployed alongside external dependencies.
    Treat the project root as an implicit local package: any primitives
    found in <project_root>/.apm/ are integrated after all declared
    dependency packages have been processed.

    Delegates to ``_install_mod._integrate_local_content`` which creates a
    synthetic ``_local`` APMPackage with ``PackageType.APM_PACKAGE`` so that
    a root-level ``SKILL.md`` is NOT deployed as a skill.  Deployed files
    are tracked on ``ctx.local_deployed_files`` for the downstream
    post-deps-local phase (stale cleanup + lockfile persistence).

    Returns a counter-delta dict, or ``None`` if root integration is
    not applicable or failed.
    """
    if not ctx.root_has_local_primitives or not ctx.targets:
        return None

    import builtins
    from apm_cli.integration.base_integrator import BaseIntegrator

    logger = ctx.logger
    diagnostics = ctx.diagnostics

    # Track error count before local integration so the post-deps-local
    # phase can decide whether stale cleanup is safe.
    ctx.local_content_errors_before = diagnostics.error_count if diagnostics else 0

    # Build managed_files that includes old local deployed files AND
    # freshly-deployed dep files so local content wins collisions with
    # both.  This matches the pre-refactor Click handler behavior where
    # managed_files was rebuilt from the post-install lockfile.
    _local_managed = builtins.set(ctx.managed_files)
    _local_managed.update(ctx.old_local_deployed)
    for _dep_files in ctx.package_deployed_files.values():
        _local_managed.update(_dep_files)
    _local_managed = BaseIntegrator.normalize_managed_files(_local_managed)

    if logger:
        logger.download_complete("<project root>", ref_suffix="local")
        logger.verbose_detail("Integrating local .apm/ content...")
    try:
        _root_result = _install_mod._integrate_local_content(
            ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=_local_managed,
            diagnostics=diagnostics,
            logger=logger,
            scope=ctx.scope,
        )

        # Track deployed files for the post-deps-local phase (stale
        # cleanup + lockfile persistence of local_deployed_files).
        ctx.local_deployed_files = _root_result.get("deployed_files", [])

        _local_total = sum(
            _root_result.get(k, 0)
            for k in ("prompts", "agents", "skills", "sub_skills",
                      "instructions", "commands", "hooks")
        )
        if _local_total > 0 and logger:
            logger.verbose_detail(
                f"Deployed {_local_total} local primitive(s) from .apm/"
            )

        return {
            "installed": 1,
            "prompts": _root_result["prompts"],
            "agents": _root_result["agents"],
            "skills": _root_result.get("skills", 0),
            "sub_skills": _root_result.get("sub_skills", 0),
            "instructions": _root_result["instructions"],
            "commands": _root_result["commands"],
            "hooks": _root_result["hooks"],
            "links_resolved": _root_result["links_resolved"],
        }
    except Exception as e:
        import traceback as _tb
        diagnostics.error(
            f"Failed to integrate root project primitives: {e}",
            package="<root>",
            detail=_tb.format_exc(),
        )
        # When root integration is the *only* action (no external deps),
        # a failure means nothing was deployed -- surface it clearly.
        if not ctx.all_apm_deps and logger:
            logger.error(
                f"Root project primitives could not be integrated: {e}"
            )
        return None


# ======================================================================
# Public phase entry point
# ======================================================================


def run(ctx: "InstallContext") -> None:
    """Execute the sequential integration phase.

    On return the following *ctx* fields are populated / updated:
    ``installed_count``, ``unpinned_count``, ``installed_packages``,
    ``package_deployed_files``, ``package_types``, ``package_hashes``,
    ``total_prompts_integrated``, ``total_agents_integrated``,
    ``total_skills_integrated``, ``total_sub_skills_promoted``,
    ``total_instructions_integrated``, ``total_commands_integrated``,
    ``total_hooks_integrated``, ``total_links_resolved``.
    """
    # ------------------------------------------------------------------
    # Module-attribute access for late-patchability.
    # Tests patch names at apm_cli.commands.install.X -- importing the
    # MODULE (not the name) ensures the patched attribute is resolved at
    # call time.
    # ------------------------------------------------------------------
    from apm_cli.commands import install as _install_mod

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
    )

    # ------------------------------------------------------------------
    # Unpack loop-level aliases and int counters.
    # Mutable containers (lists, dicts, sets) share the reference so
    # in-place mutations by helpers are visible through ctx.  Int
    # counters are accumulated into locals and written back at the end.
    # ------------------------------------------------------------------
    deps_to_install = ctx.deps_to_install
    apm_modules_dir = ctx.apm_modules_dir

    # Int counters (written back to ctx at end of function)
    installed_count = ctx.installed_count
    unpinned_count = ctx.unpinned_count
    total_prompts_integrated = ctx.total_prompts_integrated
    total_agents_integrated = ctx.total_agents_integrated
    total_skills_integrated = ctx.total_skills_integrated
    total_sub_skills_promoted = ctx.total_sub_skills_promoted
    total_instructions_integrated = ctx.total_instructions_integrated
    total_commands_integrated = ctx.total_commands_integrated
    total_hooks_integrated = ctx.total_hooks_integrated
    total_links_resolved = ctx.total_links_resolved

    # ------------------------------------------------------------------
    # Main loop: iterate deps_to_install and dispatch to the appropriate
    # per-package helper based on package source.
    # ------------------------------------------------------------------
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}[/cyan]"),
        BarColumn(),
        TaskProgressColumn(),
        transient=True,  # Progress bar disappears when done
    ) as progress:
        for dep_ref in deps_to_install:
            # Determine installation directory using namespaced structure
            # e.g., microsoft/apm-sample-package -> apm_modules/microsoft/apm-sample-package/
            # For virtual packages: owner/repo/prompts/file.prompt.md -> apm_modules/owner/repo-file/
            # For subdirectory packages: owner/repo/subdir -> apm_modules/owner/repo/subdir/
            if dep_ref.alias:
                # If alias is provided, use it directly (assume user handles namespacing)
                install_path = apm_modules_dir / dep_ref.alias
            else:
                # Use the canonical install path from DependencyReference
                install_path = dep_ref.get_install_path(apm_modules_dir)

            # Skip deps that already failed during BFS resolution callback
            # to avoid a duplicate error entry in diagnostics.
            dep_key = dep_ref.get_unique_key()
            if dep_key in ctx.callback_failures:
                if ctx.logger:
                    ctx.logger.verbose_detail(f"  Skipping {dep_key} (already failed during resolution)")
                continue

            # --- Dispatch to per-source helper ---
            if dep_ref.is_local and dep_ref.local_path:
                deltas = _integrate_local_dep(
                    ctx, _install_mod, dep_ref, install_path, dep_key,
                )
            else:
                resolved_ref, skip_download, dep_locked_chk, ref_changed = (
                    _resolve_download_strategy(ctx, dep_ref, install_path)
                )
                if skip_download:
                    deltas = _integrate_cached_dep(
                        ctx, _install_mod, dep_ref, install_path, dep_key,
                        resolved_ref, dep_locked_chk,
                    )
                else:
                    deltas = _integrate_fresh_dep(
                        ctx, _install_mod, dep_ref, install_path, dep_key,
                        resolved_ref, dep_locked_chk, ref_changed, progress,
                    )

            if deltas is None:
                continue

            # Accumulate counter deltas from this package
            installed_count += deltas.get("installed", 0)
            unpinned_count += deltas.get("unpinned", 0)
            total_prompts_integrated += deltas.get("prompts", 0)
            total_agents_integrated += deltas.get("agents", 0)
            total_skills_integrated += deltas.get("skills", 0)
            total_sub_skills_promoted += deltas.get("sub_skills", 0)
            total_instructions_integrated += deltas.get("instructions", 0)
            total_commands_integrated += deltas.get("commands", 0)
            total_hooks_integrated += deltas.get("hooks", 0)
            total_links_resolved += deltas.get("links_resolved", 0)

    # ------------------------------------------------------------------
    # Integrate root project's own .apm/ primitives (#714).
    # ------------------------------------------------------------------
    root_deltas = _integrate_root_project(ctx, _install_mod)
    if root_deltas:
        installed_count += root_deltas.get("installed", 0)
        total_prompts_integrated += root_deltas.get("prompts", 0)
        total_agents_integrated += root_deltas.get("agents", 0)
        total_skills_integrated += root_deltas.get("skills", 0)
        total_sub_skills_promoted += root_deltas.get("sub_skills", 0)
        total_instructions_integrated += root_deltas.get("instructions", 0)
        total_commands_integrated += root_deltas.get("commands", 0)
        total_hooks_integrated += root_deltas.get("hooks", 0)
        total_links_resolved += root_deltas.get("links_resolved", 0)

    # ------------------------------------------------------------------
    # Write int counters back to ctx (mutable containers already share
    # the reference and need no write-back).
    # ------------------------------------------------------------------
    ctx.installed_count = installed_count
    ctx.unpinned_count = unpinned_count
    ctx.total_prompts_integrated = total_prompts_integrated
    ctx.total_agents_integrated = total_agents_integrated
    ctx.total_skills_integrated = total_skills_integrated
    ctx.total_sub_skills_promoted = total_sub_skills_promoted
    ctx.total_instructions_integrated = total_instructions_integrated
    ctx.total_commands_integrated = total_commands_integrated
    ctx.total_hooks_integrated = total_hooks_integrated
    ctx.total_links_resolved = total_links_resolved
