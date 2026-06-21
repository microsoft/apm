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
from typing import TYPE_CHECKING, Any

from apm_cli.install.phases._redownload import _should_skip_redownload
from apm_cli.install.phases._skip_logic import _compute_skip_download
from apm_cli.install.phases.heal import run_heal_chain
from apm_cli.install.services import integrate_local_content
from apm_cli.install.sources import make_dependency_source
from apm_cli.install.template import run_integration_template

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


# ======================================================================
# Private helpers -- each encapsulates one per-package integration path
# ======================================================================


def _resolve_download_strategy(
    ctx: InstallContext,
    dep_ref: Any,
    install_path: Path,
) -> tuple[Any, bool, Any, bool]:
    """Determine whether *dep_ref* can be served from cache.

    Returns ``(resolved_ref, skip_download, dep_locked_chk, ref_changed)``
    where *skip_download* is ``True`` when the package at *install_path*
    is already up-to-date.
    """
    from apm_cli.drift import detect_ref_change
    from apm_cli.models.apm_package import GitReferenceType
    from apm_cli.utils.path_security import safe_rmtree

    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    diagnostics = ctx.diagnostics
    logger = ctx.logger
    dep_key = dep_ref.get_unique_key()

    # npm-like behavior: Branches always fetch latest, only tags/commits use cache
    # Resolve git reference to determine type
    resolved_ref = None
    # Registry-sourced deps don't have a git reference to resolve — calling
    # ``resolve_git_reference`` on them would issue ``git ls-remote`` against
    # the dep's notional host (default github.com), which can trigger an SSH
    # key-acceptance prompt or a wasted network call. Skip the git probe
    # entirely for non-git sources.
    _source = getattr(dep_ref, "source", None)
    is_git_source = not isinstance(_source, str) or _source in (None, "git")
    if is_git_source and dep_key not in ctx.pre_downloaded_keys:
        # Resolve when there is an explicit ref, OR when update_refs
        # is True AND we have a non-cached lockfile entry to compare
        # against (otherwise resolution is wasted work -- the package
        # will be downloaded regardless).
        _has_lockfile_sha = False
        if update_refs and existing_lockfile:
            _lck = existing_lockfile.get_dependency(dep_key)
            _has_lockfile_sha = bool(
                _lck and _lck.resolved_commit and _lck.resolved_commit != "cached"
            )
        if dep_ref.reference or (update_refs and _has_lockfile_sha):
            try:  # noqa: SIM105
                resolved_ref = ctx.downloader.resolve_git_reference(dep_ref)
            except Exception:
                pass  # If resolution fails, skip cache (fetch latest)

    # Use cache only for tags and commits (not branches)
    is_cacheable = resolved_ref and resolved_ref.ref_type in [
        GitReferenceType.TAG,
        GitReferenceType.COMMIT,
    ]
    # Skip download if: already fetched by resolver callback, or cached tag/commit
    already_resolved = dep_key in ctx.callback_downloaded
    # Detect if manifest ref changed vs what the lockfile recorded.
    # detect_ref_change() handles all transitions including None->ref.
    _dep_locked_chk = existing_lockfile.get_dependency(dep_key) if existing_lockfile else None
    ref_changed = detect_ref_change(dep_ref, _dep_locked_chk, update_refs=update_refs)
    # When the manifest ref drifted from the lockfile, the content hash
    # will legitimately change after re-download.  Mark the dep so the
    # supply-chain check in sources.py doesn't treat it as an attack.
    if ref_changed:
        # resolve.py's BFS callback may have already added this;
        # set semantics make double-add safe.
        ctx.expected_hash_change_deps.add(dep_key)
    # Phase 5 (#171): Also skip when lockfile SHA matches local HEAD
    # -- but not when the manifest ref has changed (user wants different version).
    lockfile_match = False
    content_hash_already_verified = dep_key in ctx.content_hash_verified_deps
    # Track whether lockfile_match was satisfied via content-hash fallback only
    # (no git HEAD verification possible -- typical for virtual packages, where
    # install_path is a carved-out subdirectory rather than a git repo).
    # The self-heal logic below uses this to recover from the v<=0.12.2
    # branch-ref drift bug for upgrading users.
    lockfile_match_via_content_hash_only = False
    if install_path.exists() and existing_lockfile:
        locked_dep = existing_lockfile.get_dependency(dep_key)
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
                        # Git check failed (e.g. .git removed, or virtual
                        # package install_path is not a git repo). Fall back
                        # to content-hash verification (#763).
                        if content_hash_already_verified or _should_skip_redownload(
                            locked_dep, install_path
                        ):
                            lockfile_match = True
                            lockfile_match_via_content_hash_only = True
                            content_hash_already_verified = True
                            ctx.content_hash_verified_deps.add(dep_key)
            elif not ref_changed:
                # Normal mode: compare local HEAD with lockfile SHA.
                try:
                    from git import Repo as GitRepo

                    local_repo = GitRepo(install_path)
                    if local_repo.head.commit.hexsha == locked_dep.resolved_commit:
                        lockfile_match = True
                except Exception:
                    # Git check failed (e.g. .git removed, or virtual package
                    # install_path is not a git repo). Fall back to
                    # content-hash verification (#763).
                    if content_hash_already_verified or _should_skip_redownload(
                        locked_dep, install_path
                    ):
                        lockfile_match = True
                        lockfile_match_via_content_hash_only = True
                        content_hash_already_verified = True
                        ctx.content_hash_verified_deps.add(dep_key)
        elif (
            locked_dep
            and getattr(locked_dep, "source", None) == "registry"
            and not ref_changed
            and not update_refs
        ):
            # Registry deps have no resolved_commit; use content_hash as the
            # skip-download signal (mirrors the git content-hash fallback above).
            if content_hash_already_verified or _should_skip_redownload(locked_dep, install_path):
                lockfile_match = True
                content_hash_already_verified = True
                ctx.content_hash_verified_deps.add(dep_key)
        elif locked_dep and locked_dep.content_hash and not ref_changed and not update_refs:
            # Unpinned git/virtual deps (#1548): the lockfile recorded a
            # content_hash but no resolved_commit (e.g. ADO partial-clone
            # fallback could not pin a SHA, or a virtual-file dep was carved
            # out without a commit anchor).  Without this branch the second
            # install would re-download every time, and a non-deterministic
            # fresh hash trips the supply-chain mismatch check at
            # sources.py with a false-positive attack alert.
            #
            # Cache-skip parity with the resolved_commit branches: when the
            # on-disk content still hashes to the lockfile-recorded value,
            # the package is intact -- skip re-download. This branch has no
            # commit anchor; the content hash is the only trust signal, so any
            # divergence must fall through to the fresh-download path and its
            # supply-chain mismatch check.
            if content_hash_already_verified or _should_skip_redownload(locked_dep, install_path):
                lockfile_match = True
                lockfile_match_via_content_hash_only = True
                content_hash_already_verified = True
                ctx.content_hash_verified_deps.add(dep_key)

    # Self-heal pipeline (PR #1158).
    #
    # All install-time heals (branch-ref drift detection, v<=0.12.2
    # buggy-lockfile recovery, future heals) live in
    # ``apm_cli.install.heals`` and are dispatched by ``run_heal_chain``.
    # Each heal is an isolated, individually-testable Chain-of-
    # Responsibility handler that may turn ``lockfile_match`` False,
    # set ``ref_changed`` True, and add a bypass key telling
    # ``FreshDependencySource`` that an upcoming content_hash change is
    # legitimate recovery, not a supply-chain attack.
    #
    # The dispatcher (not individual heals) renders user-facing
    # diagnostics + log messages, so heals stay pure and testable.
    lockfile_match, ref_changed = run_heal_chain(
        ctx,
        dep_ref,
        resolved_ref=resolved_ref,
        existing_lockfile=existing_lockfile,
        lockfile_match=lockfile_match,
        lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
        update_refs=update_refs,
        ref_changed=ref_changed,
    )

    # Issue #551: skip re-download when the BFS callback already fetched this
    # dep during resolution AND the remote SHA still matches what was captured.
    # This eliminates redundant network I/O in --update mode when apm_modules/
    # is empty but the lockfile SHA is stale: the callback downloads the latest
    # content (recording its SHA), and the sequential loop would otherwise
    # re-download identical bytes because lockfile_match=False (stale SHA).
    _callback_sha = ctx.callback_downloaded.get(dep_key)
    _already_resolved_sha_match = (
        already_resolved
        and update_refs
        and bool(resolved_ref)
        and bool(_callback_sha)
        and getattr(resolved_ref, "resolved_commit", None) not in (None, "cached")
        and _callback_sha == resolved_ref.resolved_commit
    )

    if _already_resolved_sha_match and logger:
        logger.verbose_detail(f"  {dep_key}: callback SHA matches remote -- skipping re-download")

    skip_download = _already_resolved_sha_match or _compute_skip_download(
        install_path_exists=install_path.exists(),
        is_cacheable=is_cacheable,
        update_refs=update_refs,
        already_resolved=already_resolved,
        lockfile_match=lockfile_match,
    )

    # Verify content integrity when lockfile has a hash.
    # NOTE: when _already_resolved_sha_match is True, the callback has already
    # written the correct content for the current remote SHA -- but the lockfile
    # content_hash still refers to the *previous* content. If the remote content
    # changed (which is the typical stale-lockfile scenario), verify_package_hash
    # will mismatch, safe_rmtree fires, and skip_download resets to False, causing
    # a re-download. This is the correct safety behaviour but means the
    # optimisation is a no-op for update scenarios where content_hash is present
    # and stale. A follow-up can target this by propagating the callback-downloaded
    # content_hash into the verified set before this guard runs.
    if (
        skip_download
        and _dep_locked_chk
        and _dep_locked_chk.content_hash
        and not content_hash_already_verified
    ):
        from apm_cli.utils.content_hash import verify_package_hash

        if not verify_package_hash(install_path, _dep_locked_chk.content_hash):
            _hash_msg = f"Content hash mismatch for {dep_ref.get_unique_key()} -- re-downloading"
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
    ctx: InstallContext,
) -> dict[str, int] | None:
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
            source_root=ctx.source_root,
            ctx=ctx,
        )

        # Track deployed files for the post-deps-local phase (stale
        # cleanup + lockfile persistence of local_deployed_files).
        ctx.local_deployed_files = _root_result.get("deployed_files", [])

        _local_total = sum(
            _root_result.get(k, 0)
            for k in (
                "prompts",
                "agents",
                "skills",
                "sub_skills",
                "instructions",
                "commands",
                "hooks",
                "canvases",
            )
        )
        if _local_total > 0 and logger:
            logger.verbose_detail(f"Deployed {_local_total} local primitive(s) from .apm/")

        return {
            "installed": int(_local_total > 0),
            "prompts": _root_result["prompts"],
            "agents": _root_result["agents"],
            "skills": _root_result.get("skills", 0),
            "sub_skills": _root_result.get("sub_skills", 0),
            "instructions": _root_result["instructions"],
            "commands": _root_result["commands"],
            "hooks": _root_result["hooks"],
            "canvases": _root_result.get("canvases", 0),
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
            logger.error(f"Root project primitives could not be integrated: {e}")
        return None


# ======================================================================
# Cowork cap checks (Amendment 7)
# ======================================================================

_COWORK_MAX_SKILLS: int = 50
"""Warn when the cowork skills directory contains more than this many skills."""

_COWORK_MAX_SKILL_SIZE: int = 1_048_576  # 1 MB
"""Warn when any source SKILL.md exceeds this size in bytes."""


def _check_cowork_caps(ctx: InstallContext) -> None:
    """Emit warn-only diagnostics for cowork skill count and size caps.

    Walks ``<cowork_root>/skills/*/SKILL.md`` (existing + just-installed)
    and checks against ``_COWORK_MAX_SKILLS`` and ``_COWORK_MAX_SKILL_SIZE``.
    Install still succeeds regardless.
    """
    if not ctx.targets:
        return

    cowork_root = None
    for t in ctx.targets:
        if t.name == "copilot-cowork" and t.resolved_deploy_root is not None:
            cowork_root = t.resolved_deploy_root
            break
    if cowork_root is None:
        return
    if not cowork_root.is_dir():
        return

    skill_dirs = sorted(
        d for d in cowork_root.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )

    # --- count cap ---
    if len(skill_dirs) > _COWORK_MAX_SKILLS:
        msg = (
            f"Cowork skills directory contains {len(skill_dirs)} skills "
            f"(cap: {_COWORK_MAX_SKILLS}). Consider removing unused skills."
        )
        if ctx.logger:
            ctx.logger.warning(msg, symbol="warning")
        if ctx.diagnostics:
            ctx.diagnostics.warn(msg, package="cowork")

    # --- per-file size cap ---
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        try:
            size = skill_md.stat().st_size
        except OSError:
            continue
        if size > _COWORK_MAX_SKILL_SIZE:
            size_mb = size / (1024 * 1024)
            msg = (
                f"Skill '{skill_dir.name}/SKILL.md' is {size_mb:.1f} MB "
                f"(cap: 1 MB). Large skills may degrade Copilot performance."
            )
            if ctx.logger:
                ctx.logger.warning(msg, symbol="warning")
            if ctx.diagnostics:
                ctx.diagnostics.warn(msg, package="cowork")


def _run_executable_approval_prompt(ctx: InstallContext) -> None:
    """Prompt for approval of packages whose executables were blocked.

    After the integration loop, any package that had hooks or bin/
    blocked is collected in ``ctx.blocked_executables``.  This function
    runs the interactive approval flow (or hard-errors in CI) and
    persists approved entries to the personal ``~/.apm/config.json``
    ``executables`` block (lowest authority, never committed to source
    control) so the next install deploys them (#1873).
    """
    if not ctx.blocked_executables:
        return

    from apm_cli.security.executables import (
        load_user_executables,
        prompt_executable_approval,
        save_user_executables,
    )

    # Seed the prompt with existing personal consent (not project entries,
    # which are read-only from the install pipeline's perspective).
    allow_exec, deny_exec = load_user_executables()

    updated = prompt_executable_approval(
        ctx.blocked_executables,
        allow_executables=allow_exec,
    )

    # Persist new approvals to the personal config if the user approved
    # anything new.
    if updated and updated != allow_exec:
        save_user_executables(updated, deny_exec)
        if ctx.logger:
            ctx.logger.info(
                "Updated ~/.apm/config.json. "
                "Run 'apm install' again to deploy approved executables.",
                symbol="info",
            )


# ======================================================================
# Public phase entry point
# ======================================================================


def run(ctx: InstallContext) -> None:
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
    # Unpack loop-level aliases and int counters.
    # Mutable containers (lists, dicts, sets) share the reference so
    # in-place mutations by helpers are visible through ctx.  Int
    # counters are accumulated into locals and written back at the end.
    # ------------------------------------------------------------------
    deps_to_install = ctx.deps_to_install
    apm_modules_dir = ctx.apm_modules_dir

    # Direct dep keys: used to distinguish direct vs transitive failures
    # so direct failures can be surfaced immediately.
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)

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
    # per-package helper based on package source.  Per-dep progress is
    # routed through ``ctx.tui`` (workstream B, #1116); when the TUI is
    # disabled every method is a no-op.
    # ------------------------------------------------------------------
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
                ctx.logger.verbose_detail(
                    f"  Skipping {dep_key} (already failed during resolution)"
                )
            continue

        # --- Build the right DependencySource and run the template ---
        if dep_ref.is_local and dep_ref.local_path:
            source = make_dependency_source(
                ctx,
                dep_ref,
                install_path,
                dep_key,
            )
        else:
            resolved_ref, skip_download, dep_locked_chk, ref_changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )
            # F2 (#1116): when the resolver callback already
            # downloaded this package during the parallel resolve
            # phase, ``skip_download`` will be True but the bytes
            # arrived in this run. Tell the cached source so it
            # does not falsely tag the line ``(cached)``.
            _fetched_now = dep_key in ctx.callback_downloaded
            source = make_dependency_source(
                ctx,
                dep_ref,
                install_path,
                dep_key,
                resolved_ref=resolved_ref,
                dep_locked_chk=dep_locked_chk,
                ref_changed=ref_changed,
                skip_download=skip_download,
                fetched_this_run=_fetched_now,
            )

        deltas = run_integration_template(source)

        if deltas is None:
            # Direct dependency failure: surface a single concise
            # inline marker so the user sees `[x] <pkg>: integration
            # failed` immediately (fixes "perceived hang" on HYBRID
            # validation failures). The full diagnostic detail --
            # resolved path and `--verbose` hint -- is rendered once
            # by `render_summary()` to avoid double-output.
            if dep_key in direct_dep_keys:
                if ctx.diagnostics:
                    ctx.diagnostics.error(
                        f"{dep_key}: integration failed",
                        package=dep_key,
                        detail=(f"Resolved at {install_path}. Run with --verbose for details."),
                    )
                elif ctx.logger:
                    ctx.logger.error(f"{dep_key}: integration failed")
                ctx.direct_dep_failed = True
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

    # ------------------------------------------------------------------
    # Amendment 7: cowork 50-skill / 1 MB cap check (warn-only).
    # Runs once per install, after all packages integrate, only when
    # a cowork target with a resolved_deploy_root is active.
    # ------------------------------------------------------------------
    _check_cowork_caps(ctx)

    # ------------------------------------------------------------------
    # Executable approval prompt: if any packages had their hooks or
    # bin/ blocked, prompt the user to approve them (interactive) or
    # hard-error (CI).  Approved packages are persisted to apm.yml so
    # the next ``apm install`` deploys them automatically.
    # ------------------------------------------------------------------
    _run_executable_approval_prompt(ctx)
