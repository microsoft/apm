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

Implementation note
-------------------
The three cohesive helper groups have been extracted to private sibling
modules to keep this file under 500 lines.  All names remain importable
from *this* module (re-exported below) so that existing test import
paths and ``unittest.mock.patch`` targets are unchanged:

* ``_resolve_download_strategy`` -- ``._lockfile_check``
* ``_check_cowork_caps``          -- ``._cowork_caps``
* ``_integrate_root_project``     -- ``._root_project``
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

# All first-party imports are kept in one sorted block so ruff/isort is
# satisfied.  Inline notes explain the two constraints:
#
# (1) make_dependency_source and run_integration_template MUST be
#     module-level names here so that unittest.mock.patch targets
#     ``apm_cli.install.phases.integrate.{name}`` work without test changes.
#
# (2) _resolve_download_strategy, _check_cowork_caps, and
#     _integrate_root_project are re-exported from their private sibling
#     modules so that direct test imports such as
#     ``from apm_cli.install.phases.integrate import _resolve_download_strategy``
#     continue to resolve here.
from apm_cli.install.phases._cowork_caps import _check_cowork_caps  # (2)
from apm_cli.install.phases._lockfile_check import _resolve_download_strategy  # (2)
from apm_cli.install.phases._root_project import _integrate_root_project  # (2)
from apm_cli.install.sources import make_dependency_source  # (1)
from apm_cli.install.template import run_integration_template  # (1)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


# ======================================================================
# Public phase entry point
# ======================================================================


def _accumulate_counter_deltas(ctx: InstallContext, deltas: dict[str, int]) -> None:
    ctx.installed_count += deltas.get("installed", 0)
    ctx.unpinned_count += deltas.get("unpinned", 0)
    ctx.total_prompts_integrated += deltas.get("prompts", 0)
    ctx.total_agents_integrated += deltas.get("agents", 0)
    ctx.total_skills_integrated += deltas.get("skills", 0)
    ctx.total_sub_skills_promoted += deltas.get("sub_skills", 0)
    ctx.total_instructions_integrated += deltas.get("instructions", 0)
    ctx.total_commands_integrated += deltas.get("commands", 0)
    ctx.total_hooks_integrated += deltas.get("hooks", 0)
    ctx.total_links_resolved += deltas.get("links_resolved", 0)


def _handle_dep_failure(
    ctx: InstallContext,
    dep_key: str,
    install_path,
    direct_dep_keys: set[str],
) -> None:
    if dep_key not in direct_dep_keys:
        return
    if ctx.diagnostics:
        ctx.diagnostics.error(
            f"{dep_key}: integration failed",
            package=dep_key,
            detail=(f"Resolved at {install_path}. Run with --verbose for details."),
        )
    elif ctx.logger:
        ctx.logger.error(f"{dep_key}: integration failed")
    ctx.direct_dep_failed = True


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

    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)

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
            _handle_dep_failure(ctx, dep_key, install_path, direct_dep_keys)
            continue

        _accumulate_counter_deltas(ctx, deltas)

    # ------------------------------------------------------------------
    # Integrate root project's own .apm/ primitives (#714).
    # ------------------------------------------------------------------
    root_deltas = _integrate_root_project(ctx)
    if root_deltas:
        _accumulate_counter_deltas(ctx, root_deltas)

    # ------------------------------------------------------------------
    # Amendment 7: cowork 50-skill / 1 MB cap check (warn-only).
    # Runs once per install, after all packages integrate, only when
    # a cowork target with a resolved_deploy_root is active.
    # ------------------------------------------------------------------
    _check_cowork_caps(ctx)
