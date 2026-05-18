"""Click commands for ``apm pack`` and ``apm unpack``."""

import sys
from pathlib import Path

import click

from ..bundle.unpacker import unpack_bundle
from ..core.build_orchestrator import BuildError, BuildOptions, BuildOrchestrator, OutputKind
from ..core.command_logger import CommandLogger
from ..core.target_detection import TargetParamType
from ..utils.console import set_console_stderr
from .pack_helpers import (
    _emit_json_error_or_raise,
    _emit_pack_json,
    _log_bundle_meta,
    _log_unpack_file_list,
    _parse_marketplace_filter,
    _parse_marketplace_path_overrides,
    _render_bundle_result,
    _render_marketplace_result,
    _resolve_effective_target,
)

MARKETPLACE_DOCS_URL = (
    "https://microsoft.github.io/apm/producer/publish-to-a-marketplace/#consume-from-any-assistant"
)

_PACK_HELP = """\
Pack distributable artifacts from your APM project.

Reads apm.yml to decide what to produce:

  dependencies: block  ->  bundle (directory or .tar.gz)
  marketplace: block   ->  selected marketplace artifacts
  both blocks present  ->  bundle plus selected marketplace artifacts

The lockfile (apm.lock.yaml) pins bundle contents. An enriched copy
is embedded in each bundle.

Examples:

  # Bundle only (most common -- just dependencies: in apm.yml):
  apm pack                              # Claude Code plugin (default)
  apm pack --target claude --archive
  apm pack --format apm -o ./dist       # Legacy APM bundle layout

  # Marketplace only (marketplace: in apm.yml, no dependencies:):
  apm pack
  apm pack --offline --dry-run

  # Both (apm.yml has dependencies: AND marketplace: blocks):
  apm pack
  apm pack --archive --offline

  # Marketplace output paths are normally configured in apm.yml:
  # marketplace.claude.output / marketplace.codex.output

Exit codes:
  0  Success
  1  Build or runtime error
  2  Manifest schema validation error
  3  Version alignment check failed (--check-versions)
  4  Marketplace working-tree drift detected (--check-clean)
"""


@click.command(name="pack", help=_PACK_HELP)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["plugin", "apm"]),
    default="plugin",
    help="Bundle format. 'plugin' (default) emits a Claude Code plugin directory with plugin.json. 'apm' produces the legacy APM bundle layout (kept for tooling that still consumes it).",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="[Deprecated] Target platform filter. Bundles are now target-agnostic; the consumer's project decides where files land at install time. Value is recorded in pack.target as informational metadata only and is ignored by 'apm install'. The flag will be removed in a future release.",
)
@click.option(
    "--archive",
    is_flag=True,
    default=False,
    help="Produce a .tar.gz archive instead of a directory.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default="./build",
    help="Bundle output directory (default: ./build).",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would be packed without writing"
)
@click.option(
    "--force", is_flag=True, default=False, help="On collision (plugin format), last writer wins."
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed packing information.")
@click.option(
    "--offline",
    is_flag=True,
    default=False,
    help="Marketplace: use cached refs, skip network.",
)
@click.option(
    "--include-prerelease",
    is_flag=True,
    default=False,
    help="Marketplace: include pre-release version tags.",
)
@click.option(
    "--check-versions",
    is_flag=True,
    default=False,
    help=(
        "Release gate: verify per-package versions agree with the configured "
        "marketplace.versioning.strategy (lockstep | tag_pattern | per_package). "
        "Exits 3 on misalignment. Composes with --check-clean and --dry-run."
    ),
)
@click.option(
    "--check-clean",
    is_flag=True,
    default=False,
    help=(
        "Release gate: regenerate every configured marketplace output to a "
        "temp path and diff against the on-disk file. Exits 4 if the working "
        "tree is dirty (out-of-date marketplace.json). The gate itself "
        "never writes to disk."
    ),
)
@click.option(
    "--marketplace-output",
    "marketplace_output",
    type=click.Path(),
    default=None,
    hidden=True,
    help=("[Deprecated] Override Claude output path. Use --marketplace-path claude=PATH instead."),
)
@click.option(
    "-m",
    "--marketplace",
    "marketplace_filter",
    type=str,
    default=None,
    help=(
        "Comma-separated marketplace outputs to build (e.g. 'claude,codex'). "
        "Use 'all' for every configured output, 'none' to skip marketplace. "
        "Default: build all configured outputs."
    ),
)
@click.option(
    "--marketplace-path",
    "marketplace_path_overrides",
    type=str,
    multiple=True,
    help=(
        "Override output path for a format: FORMAT=PATH (repeatable). "
        "Example: --marketplace-path claude=dist/marketplace.json"
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON to stdout; logs go to stderr.",
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Compatibility flag for projects that "
        "need per-client skill layouts."
    ),
)
@click.pass_context
def pack_cmd(
    ctx,
    fmt,
    target,
    archive,
    output,
    dry_run,
    force,
    verbose,
    offline,
    include_prerelease,
    marketplace_output,
    marketplace_filter,
    marketplace_path_overrides,
    json_output,
    legacy_skill_paths,
    check_versions,
    check_clean,
):
    """Pack APM artifacts: bundle and/or marketplace.json."""
    if json_output:
        set_console_stderr(True)

    logger = CommandLogger("pack", verbose=verbose, dry_run=dry_run)
    if marketplace_output is not None:
        translated = f"--marketplace-path claude={marketplace_output}"
        click.echo(
            f"Warning: --marketplace-output is deprecated and will be removed in v0.15. "
            f"Use {translated} instead.",
            err=True,
        )
        marketplace_path_overrides = (*marketplace_path_overrides, f"claude={marketplace_output}")

    path_overrides = _parse_marketplace_path_overrides(
        ctx,
        json_output,
        marketplace_path_overrides,
    )
    marketplace_formats = _parse_marketplace_filter(ctx, json_output, marketplace_filter)
    project_root = Path(".").resolve()
    effective_target = _resolve_effective_target(project_root, target, logger)
    options = BuildOptions(
        project_root=project_root,
        apm_yml_path=project_root / "apm.yml",
        bundle_format=fmt,
        bundle_target=effective_target,
        bundle_archive=archive,
        bundle_output=Path(output),
        bundle_force=force,
        marketplace_offline=offline,
        marketplace_include_prerelease=include_prerelease,
        marketplace_output=None,
        marketplace_formats=marketplace_formats,
        marketplace_path_overrides=path_overrides or None,
        dry_run=dry_run,
        verbose=verbose,
    )

    try:
        result = BuildOrchestrator().run(options, logger=logger)
    except BuildError as exc:
        _emit_json_error_or_raise(ctx, json_output, "build_error", str(exc))
        return

    # -- Release gate checks --
    version_alignment_payload: dict | None = None
    drift_payload: dict | None = None
    gate_errors: list[dict] = []
    version_gate_failed = False
    drift_gate_failed = False

    if check_versions or check_clean:
        from ..marketplace.builder import BuildOptions as MktBuildOptions
        from ..marketplace.builder import MarketplaceBuilder
        from ..marketplace.drift_check import check_marketplace_drift, render_diff_lines
        from ..marketplace.migration import (
            ConfigSource,
            detect_config_source,
        )
        from ..marketplace.version_check import check_version_alignment
        from ..marketplace.yml_schema import MarketplaceYmlError

        gate_config = None
        try:
            source = detect_config_source(project_root)
            if source != ConfigSource.NONE:
                from ..marketplace.migration import load_marketplace_config

                gate_config = load_marketplace_config(project_root)
        except MarketplaceYmlError as exc:
            _emit_json_error_or_raise(ctx, json_output, "build_error", str(exc))
            return

        if gate_config is None:
            if check_versions:
                logger.info(
                    "Version alignment check skipped: no marketplace block; nothing to check."
                )
            if check_clean:
                logger.info(
                    "Marketplace drift check skipped: no marketplace block; nothing to check."
                )
        else:
            if check_versions:
                v_report = check_version_alignment(gate_config, project_root)
                version_alignment_payload = v_report.to_json_dict()
                if v_report.ok:
                    if not json_output:
                        if v_report.expected is not None:
                            logger.success(
                                f"Version alignment OK [strategy={v_report.strategy}, "
                                f"expected={v_report.expected}]"
                            )
                        else:
                            logger.success(f"Version alignment OK [strategy={v_report.strategy}]")
                        for row in v_report.packages:
                            tag_str = f"  -> tag {row.rendered_tag}" if row.rendered_tag else ""
                            logger.info(f"    {row.path}  {row.version}{tag_str}  [{row.reason}]")
                else:
                    version_gate_failed = True
                    if not json_output:
                        if v_report.expected is not None:
                            logger.error(
                                f"Version alignment failed [strategy={v_report.strategy}, "
                                f"expected={v_report.expected}]"
                            )
                        else:
                            logger.error(f"Version alignment failed [strategy={v_report.strategy}]")
                        for row in v_report.packages:
                            tag_str = f"  -> tag {row.rendered_tag}" if row.rendered_tag else ""
                            version_str = row.version if row.version is not None else "<none>"
                            logger.info(f"    {row.path}  {version_str}{tag_str}  [{row.reason}]")
                    for msg in v_report.error_messages():
                        gate_errors.append({"code": "version_misaligned", "message": msg})

            if check_clean:
                mkt_opts = MktBuildOptions(
                    dry_run=True,
                    offline=options.marketplace_offline,
                    include_prerelease=options.marketplace_include_prerelease,
                    marketplace_output=None,
                )
                drift_builder = MarketplaceBuilder.from_config(
                    gate_config, project_root=project_root, options=mkt_opts
                )
                d_report = check_marketplace_drift(drift_builder, gate_config, project_root)
                drift_payload = d_report.to_json_dict()
                if d_report.ok:
                    if not json_output:
                        formats = ", ".join(o.format for o in d_report.outputs)
                        logger.success(f"Marketplace working tree clean [outputs={formats}]")
                        for out in d_report.outputs:
                            logger.info(f"    {out.path}  [unchanged]")
                else:
                    drift_gate_failed = True
                    if not json_output:
                        dirty_formats = ", ".join(
                            o.format for o in d_report.outputs if o.status != "unchanged"
                        )
                        logger.error(f"Marketplace working tree dirty [outputs={dirty_formats}]")
                        for out in d_report.outputs:
                            if out.status == "unchanged":
                                logger.info(f"    {out.path}  [unchanged]")
                            elif out.status == "missing":
                                logger.info(f"    {out.path}  [missing on disk; would be created]")
                            else:
                                count = len(out.differences)
                                logger.info(f"    {out.path}  [drift: {count} differences]")
                                for line in render_diff_lines(out):
                                    logger.info(line)
                    for msg in d_report.error_messages():
                        gate_errors.append({"code": "marketplace_drift", "message": msg})

    # -- JSON output mode: consistent envelope --
    if json_output:
        import json as json_mod

        envelope = {
            "ok": True,
            "dry_run": dry_run,
            "warnings": [],
            "errors": [],
            "marketplace": {"outputs": []},
            "bundle": None,
            "version_alignment": version_alignment_payload,
            "drift": drift_payload,
        }
        for sub in result.producer_results:
            if sub.kind is OutputKind.MARKETPLACE and sub.payload is not None:
                payload = sub.payload.to_json_dict()
                envelope["warnings"] = payload.get("warnings", [])
                envelope["marketplace"] = payload.get("marketplace", {"outputs": []})
                break
        if gate_errors:
            envelope["errors"] = list(envelope["errors"]) + gate_errors
            envelope["ok"] = False
        click.echo(json_mod.dumps(envelope, indent=2))
        if version_gate_failed:
            ctx.exit(3)
        if drift_gate_failed:
            ctx.exit(4)
        return

    for sub in result.producer_results:
        if sub.kind is OutputKind.BUNDLE:
            _render_bundle_result(logger, sub.payload, fmt, target, dry_run)
        elif sub.kind is OutputKind.MARKETPLACE:
            _render_marketplace_result(logger, sub.payload, dry_run, sub.warnings, sub.outputs)

    # Gate exit codes (after non-JSON rendering above): 3 wins over 4.
    if version_gate_failed:
        ctx.exit(3)
    if drift_gate_failed:
        ctx.exit(4)


@click.command(
    name="unpack",
    help=(
        "[Deprecated] Extract an APM bundle into the current project. "
        "Use 'apm install <bundle-path>' instead -- this command will be removed in v0.14."
    ),
)
@click.argument("bundle_path", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default=".",
    help="Target directory (default: current directory).",
)
@click.option("--skip-verify", is_flag=True, default=False, help="Skip bundle completeness check.")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would be unpacked without writing"
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Deploy despite critical hidden-character findings.",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed unpacking information")
@click.pass_context
def unpack_cmd(ctx, bundle_path, output, skip_verify, dry_run, force, verbose):
    """Extract an APM bundle into the project."""
    logger = CommandLogger("unpack", verbose=verbose, dry_run=dry_run)
    logger.warning(
        "'apm unpack' is deprecated and will be removed in v0.14. "
        "Use 'apm install <bundle-path>' instead.",
    )
    try:
        logger.start(f"Unpacking {bundle_path} -> {output}")

        result = unpack_bundle(
            bundle_path=Path(bundle_path),
            output_dir=Path(output),
            skip_verify=skip_verify,
            dry_run=dry_run,
            force=force,
        )

        # Surface bundle metadata and warn on target mismatch
        _log_bundle_meta(result, Path(output), logger)

        if dry_run:
            logger.dry_run_notice("No files written")
            if result.files:
                logger.progress(f"Would unpack {len(result.files)} file(s):")
                _log_unpack_file_list(result, logger)
            else:
                logger.warning("No files in bundle")
            return

        if not result.files:
            logger.warning("No files were unpacked")
        else:
            _log_unpack_file_list(result, logger)
            if result.skipped_count > 0:
                logger.warning(f"  {result.skipped_count} file(s) skipped (missing from bundle)")
            if result.security_critical > 0:
                logger.warning(
                    f"  Deployed with --force despite {result.security_critical} "
                    f"critical hidden-character finding(s)"
                )
            elif result.security_warnings > 0:
                logger.warning(
                    f"  {result.security_warnings} hidden-character warning(s) "
                    f"-- run 'apm audit' to inspect"
                )
            verified_msg = " (verified)" if result.verified else ""
            logger.success(f"Unpacked {len(result.files)} file(s){verified_msg}")

    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)
