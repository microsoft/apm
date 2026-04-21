"""Sequential integration phase -- per-package integration loop.

Reads all prior phase outputs from *ctx* (resolve, targets, download) and
processes each dependency sequentially.  Per-source acquisition is handled
by ``DependencySource`` Strategy implementations
(``apm_cli.install.sources``); the shared post-acquire flow (security gate
+ primitive integration + diagnostics) lives in the Template Method
``apm_cli.install.template.run_integration_template``.

After the dependency loop, root-project primitives (``<project_root>/.apm/``)
are integrated when present (#714) -- this path is structurally distinct
(no ``PackageInfo``, dedicated ``ctx.local_deployed_files`` tracking) so it
remains a sibling helper here rather than a fourth ``DependencySource``.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from apm_cli.install.services import integrate_local_content
from apm_cli.install.sources import make_dependency_source
from apm_cli.install.template import run_integration_template

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
                        # Git check failed (e.g. .git removed). Fall back to
                        # content-hash verification (#763).
                        if locked_dep.content_hash and install_path.is_dir():
                            from apm_cli.utils.content_hash import verify_package_hash
                            if verify_package_hash(install_path, locked_dep.content_hash):
                                lockfile_match = True
            elif not ref_changed:
                # Normal mode: compare local HEAD with lockfile SHA.
                try:
                    from git import Repo as GitRepo
                    local_repo = GitRepo(install_path)
                    if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                        lockfile_match = True
                except Exception:
                    # Git check failed (e.g. .git removed). Fall back to
                    # content-hash verification (#763).
                    if locked_dep.content_hash and install_path.is_dir():
                        from apm_cli.utils.content_hash import verify_package_hash
                        if verify_package_hash(install_path, locked_dep.content_hash):
                            lockfile_match = True
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



def _integrate_root_project(
    ctx: "InstallContext",
) -> Optional[Dict[str, int]]:
    """Integrate root project's own .apm/ primitives (#714).

    Users should not need a dummy "./agent/apm.yml" stub to get their
    root-level .apm/ rules deployed alongside external dependencies.
    Treat the project root as an implicit local package: any primitives
    found in <project_root>/.apm/ are integrated after all declared
    dependency packages have been processed.

    Delegates to ``integrate_local_content`` which creates a
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
        _root_result = integrate_local_content(
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

            # --- Build the right DependencySource and run the template ---
            if dep_ref.is_local and dep_ref.local_path:
                source = make_dependency_source(
                    ctx, dep_ref, install_path, dep_key,
                )
            else:
                resolved_ref, skip_download, dep_locked_chk, ref_changed = (
                    _resolve_download_strategy(ctx, dep_ref, install_path)
                )
                source = make_dependency_source(
                    ctx, dep_ref, install_path, dep_key,
                    resolved_ref=resolved_ref,
                    dep_locked_chk=dep_locked_chk,
                    ref_changed=ref_changed,
                    skip_download=skip_download,
                    progress=progress,
                )

            deltas = run_integration_template(source)

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
    root_deltas = _integrate_root_project(ctx)
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
