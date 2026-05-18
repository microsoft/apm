"""CLI handler for ``apm install <local-bundle-path>`` (issue #1098).

Extracted from :mod:`apm_cli.commands.install` to keep that module under the
architecture invariant LOC budget enforced by
``tests/unit/install/test_architecture_invariants.py``.

The handler owns the imperative deploy path for local bundles -- it does NOT
go through the dependency resolver, registry, or org-policy gate.  Local
bundles are intentionally a separate code path because they short-circuit
network I/O (proven by the air-gap E2E test).

MCP wiring (#1207): bundles MAY ship a ``.mcp.json`` (Anthropic plugin
format) describing MCP servers.  After the per-target deploy loop, the
handler routes those entries through :func:`MCPIntegrator.install` so each
resolved target's native MCP config gets the servers in its own
format/location -- ``.mcp.json`` is Claude-Code-native, but Copilot,
Cursor, OpenCode, Gemini, etc. each have their own MCP config conventions.
The bundle's ``.mcp.json`` itself is metadata and never deployed verbatim.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from ._bundle_lockfile import _migrate_legacy_skill_paths, _persist_local_bundle_lockfile
from ._bundle_mcp import _parse_bundle_mcp_servers, _wire_bundle_mcp_servers, _WireOpts


@dataclass
class _EmitSuccessCtx:
    """Bundled arguments for :func:`_emit_local_bundle_success`."""

    deployed: list
    skipped: int
    staged_instructions: list
    targets: Any
    bundle_mcp_present: bool
    dry_run: bool
    bundle_info: Any
    project_root: Path
    global_: bool
    logger: Any


def _reject_local_bundle_flags(bundle_arg: str, rejected_flags: dict[str, object]) -> None:
    """Raise when local-bundle-incompatible flags were supplied."""
    bad = [name for name, value in rejected_flags.items() if value]
    if bad:
        raise click.UsageError(
            "The following flag(s) are not valid with a local bundle install "
            f"({bundle_arg}): {', '.join(bad)}.\n"
            "Local-bundle install is an imperative deploy and does not "
            "interact with the dependency resolver, MCP, registry, or "
            "policy machinery."
        )


@dataclass(frozen=True, slots=True)
class _ResolveOpts:
    """Bundled resolve-target options for :func:`_resolve_bundle_targets`."""

    target: Any
    global_: bool
    legacy_skill_paths: bool


@dataclass(frozen=True, slots=True)
class _InstallFlags:
    """Bundled install flags for :func:`install_local_bundle`."""

    force: bool
    dry_run: bool
    logger: Any


def _resolve_bundle_targets(*, bundle_info, project_root: Path, resolve_opts: _ResolveOpts, logger):
    """Resolve install targets and apply bundle mismatch warnings."""
    from ..bundle.local_bundle import check_target_mismatch
    from ..integration.targets import apply_legacy_skill_paths, resolve_targets

    target = resolve_opts.target
    global_ = resolve_opts.global_
    legacy_skill_paths = resolve_opts.legacy_skill_paths
    explicit = target if target else None
    targets = resolve_targets(
        project_root,
        user_scope=global_,
        explicit_target=explicit,
    )
    if not targets:
        logger.warning(
            "No active targets resolved -- nothing will be deployed. "
            "Pass --target to select one explicitly."
        )
        return []

    if legacy_skill_paths:
        targets = apply_legacy_skill_paths(targets)

    warning = check_target_mismatch(
        bundle_targets=bundle_info.pack_targets,
        install_targets=[t.name for t in targets],
    )
    if warning:
        logger.warning(warning)
    return targets


def _detect_bundle_mcp_presence(bundle_info) -> bool:
    """Return whether the bundle declares a top-level .mcp.json file."""
    bundle_mcp_present = False
    if bundle_info.lockfile:
        pack = bundle_info.lockfile.get("pack") or {}
        bundle_files = pack.get("bundle_files") or {}
        if isinstance(bundle_files, dict):
            bundle_mcp_present = any(str(key).lower() == ".mcp.json" for key in bundle_files)
    if not bundle_mcp_present and bundle_info.source_dir is not None:
        bundle_mcp_present = any(
            path.name.lower() == ".mcp.json"
            for path in bundle_info.source_dir.iterdir()
            if path.is_file()
        )
    return bundle_mcp_present


def _render_local_bundle_dry_run(deployed: list[str], logger) -> None:
    """Render dry-run output for a local bundle install."""
    logger.dry_run_notice(f"Would deploy {len(deployed)} file(s) from local bundle")
    for path in deployed:
        logger.tree_item(path)


def _emit_local_bundle_success(ctx: _EmitSuccessCtx) -> None:
    """Emit post-install success messages and optional MCP wiring."""
    deployed = ctx.deployed
    skipped = ctx.skipped
    staged_instructions = ctx.staged_instructions
    targets = ctx.targets
    bundle_mcp_present = ctx.bundle_mcp_present
    dry_run = ctx.dry_run
    bundle_info = ctx.bundle_info
    project_root = ctx.project_root
    global_ = ctx.global_
    logger = ctx.logger
    msg = f"Installed {len(deployed)} file(s) from local bundle"
    if skipped:
        msg += f" ({skipped} skipped)"
    logger.success(msg)

    if staged_instructions and not dry_run:
        target_names = ", ".join(sorted({t.name for t in targets}))
        logger.warning(
            f"Bundle staged {len(staged_instructions)} instruction(s) "
            f"for compile (target: {target_names}). Run 'apm compile' "
            "to merge them into AGENTS.md / GEMINI.md / equivalent. "
            "Reference: https://microsoft.github.io/apm/guides/compilation/"
        )

    if bundle_mcp_present and not dry_run and bundle_info.source_dir is not None:
        _wire_bundle_mcp_servers(
            bundle_dir=bundle_info.source_dir,
            targets=targets,
            project_root=project_root,
            wire_opts=_WireOpts(
                user_scope=global_,
                verbose=getattr(logger, "verbose", False),
                logger=logger,
            ),
        )


def install_local_bundle(
    *,
    bundle_info,
    bundle_arg: str,
    target,
    global_: bool,
    install_flags: _InstallFlags,
    **kwargs,
) -> None:
    """Deploy a local bundle into project / user scope.

    Validates rejected flags, verifies bundle integrity, resolves install
    targets, deploys files, and persists ``local_deployed_files`` to the
    (project or user) lockfile.  Cleans up tarball extraction on exit.
    """
    verbose: bool = kwargs.get("verbose", False)
    alias: str | None = kwargs.get("alias")
    legacy_skill_paths: bool = kwargs.get("legacy_skill_paths", False)
    rejected_flags: dict[str, object] = kwargs.get("rejected_flags", {})
    force = install_flags.force
    dry_run = install_flags.dry_run
    logger = install_flags.logger
    from ..bundle.local_bundle import verify_bundle_integrity
    from ..core.scope import InstallScope
    from ..install.services import LocalBundleOpts, integrate_local_bundle

    # Reject incompatible flags with a single consolidated error.  Preserve
    # dict insertion order (matches the order options are declared on the
    # CLI command) rather than alphabetising -- M-cli-3.
    _reject_local_bundle_flags(bundle_arg, rejected_flags)

    # ``verbose`` is consumed by the InstallLogger on construction (the
    # CLI seam wires it in) -- the handler doesn't need to gate calls on
    # it because logger.verbose_detail self-gates.
    del verbose

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = Path.home() if global_ else Path.cwd()

    logger.start(f"Installing local bundle from {bundle_arg}")

    try:
        # Integrity verification (skipped when bundle has no lockfile).
        if bundle_info.lockfile is None:
            logger.warning(
                "Bundle has no apm.lock.yaml -- skipping integrity check. "
                "This bundle was produced by an older APM version."
            )
        else:
            errors = verify_bundle_integrity(bundle_info.source_dir, bundle_info.lockfile)
            if errors:
                logger.error("Bundle integrity check failed:")
                for err in errors:
                    # Plain detail lines -- no [x] symbol prefix per IM3.
                    click.echo(f"  - {err}", err=True)
                raise click.Abort()
            logger.verbose_detail("Bundle integrity verified")

        # Resolve targets and warn on bundle/install target mismatch.
        targets = _resolve_bundle_targets(
            bundle_info=bundle_info,
            project_root=project_root,
            resolve_opts=_ResolveOpts(
                target=target,
                global_=global_,
                legacy_skill_paths=legacy_skill_paths,
            ),
            logger=logger,
        )
        if not targets:
            return

        result = integrate_local_bundle(
            bundle_info,
            project_root,
            targets=targets,
            opts=LocalBundleOpts(
                force=force,
                dry_run=dry_run,
                diagnostics=None,
                logger=logger,
                scope=scope,
                alias=alias,
            ),
        )

        deployed = result.get("deployed_files", [])
        deployed_hashes = result.get("deployed_file_hashes", {})
        skipped = result.get("skipped", 0)

        # Issue #1207 D2.b: surface a compile hint when any instruction was
        # staged into ``apm_modules/<slug>/.apm/instructions/`` because the
        # resolved target lacks a native ``instructions`` primitive
        # (opencode, codex, gemini).  Without this, users would see files
        # under ``apm_modules/`` and wonder why they aren't visible to
        # their AGENTS.md / GEMINI.md.
        staged_instructions = [
            f
            for f in deployed
            if (
                f.replace("\\", "/").startswith("apm_modules/")
                and "/.apm/instructions/" in f.replace("\\", "/")
            )
        ]
        # Issue #1207 D2.c: detect bundle-level ``.mcp.json`` so the
        # post-deploy block can route it through ``MCPIntegrator.install``.
        bundle_mcp_present = _detect_bundle_mcp_presence(bundle_info)

        if dry_run:
            _render_local_bundle_dry_run(deployed, logger)
            return

        _persist_local_bundle_lockfile(
            project_root=project_root,
            deployed=deployed,
            deployed_hashes=deployed_hashes,
            legacy_skill_paths=legacy_skill_paths,
            logger=logger,
        )
        _emit_local_bundle_success(
            _EmitSuccessCtx(
                deployed=deployed,
                skipped=skipped,
                staged_instructions=staged_instructions,
                targets=targets,
                bundle_mcp_present=bundle_mcp_present,
                dry_run=dry_run,
                bundle_info=bundle_info,
                project_root=project_root,
                global_=global_,
                logger=logger,
            )
        )

    finally:
        # Tarball cleanup (caller-owned per LocalBundleInfo contract).
        if bundle_info.temp_dir is not None and bundle_info.temp_dir.exists():
            shutil.rmtree(bundle_info.temp_dir, ignore_errors=True)
