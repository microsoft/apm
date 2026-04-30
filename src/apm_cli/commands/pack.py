"""Click commands for ``apm pack`` and ``apm unpack``."""

import sys
from pathlib import Path

import click

from ..bundle.unpacker import unpack_bundle
from ..core.build_orchestrator import (
    BuildError,
    BuildOptions,
    BuildOrchestrator,
    OutputKind,
)
from ..core.command_logger import CommandLogger
from ..core.target_detection import TargetParamType


_PACK_HELP = """\
Pack distributable artifacts from your APM project.

Reads apm.yml to decide what to produce:

  dependencies: block  ->  bundle (directory or .tar.gz)
  marketplace: block   ->  .claude-plugin/marketplace.json
  both blocks present  ->  both artifacts

The lockfile (apm.lock.yaml) pins bundle contents. An enriched copy
is embedded in each bundle.

Examples:

  # Bundle only (most common -- just dependencies: in apm.yml):
  apm pack
  apm pack --target claude --archive
  apm pack --format plugin -o ./dist

  # Marketplace only (marketplace: in apm.yml, no dependencies:):
  apm pack
  apm pack --offline --dry-run

  # Both (apm.yml has dependencies: AND marketplace: blocks):
  apm pack
  apm pack --archive --offline

  # Override marketplace.json location:
  apm pack --marketplace-output ./build/marketplace.json

Exit codes:
  0  Success
  1  Build or runtime error
  2  Manifest schema validation error
"""


@click.command(name="pack", help=_PACK_HELP)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["apm", "plugin"]),
    default="apm",
    help="Bundle format: 'apm' (default) for standard bundles, 'plugin' for standalone plugin directories with plugin.json.",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated for multiple, e.g. claude,copilot). Use 'all' for every target. Auto-detects if not specified.",
)
@click.option("--archive", is_flag=True, default=False, help="Produce a .tar.gz archive instead of a directory.")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default="./build",
    help="Bundle output directory (default: ./build).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be packed without writing")
@click.option("--force", is_flag=True, default=False, help="On collision (plugin format), last writer wins.")
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
    "--marketplace-output",
    "marketplace_output",
    type=click.Path(),
    default=None,
    help="Marketplace: override output path (default: .claude-plugin/marketplace.json).",
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
):
    """Pack APM artifacts: bundle and/or marketplace.json."""
    logger = CommandLogger("pack", verbose=verbose, dry_run=dry_run)
    project_root = Path(".").resolve()
    options = BuildOptions(
        project_root=project_root,
        apm_yml_path=project_root / "apm.yml",
        bundle_format=fmt,
        bundle_target=target,
        bundle_archive=archive,
        bundle_output=Path(output),
        bundle_force=force,
        marketplace_offline=offline,
        marketplace_include_prerelease=include_prerelease,
        marketplace_output=Path(marketplace_output) if marketplace_output else None,
        dry_run=dry_run,
        verbose=verbose,
    )

    try:
        result = BuildOrchestrator().run(options, logger=logger)
    except BuildError as exc:
        raise click.ClickException(str(exc))

    for sub in result.producer_results:
        if sub.kind is OutputKind.BUNDLE:
            _render_bundle_result(logger, sub.payload, fmt, target, dry_run)
        elif sub.kind is OutputKind.MARKETPLACE:
            _render_marketplace_result(logger, sub.payload, dry_run, sub.warnings)


def _render_bundle_result(logger, pack_result, fmt, target, dry_run):
    """Mirror the legacy ``apm pack`` output for the bundle producer."""
    if pack_result is None:
        return

    mapping_summary = _mapping_summary(pack_result.path_mappings)

    if dry_run:
        if pack_result.mapped_count:
            logger.dry_run_notice(
                f"Would remap {pack_result.mapped_count} file(s){mapping_summary}"
            )
            for mapped, original in pack_result.path_mappings.items():
                logger.verbose_detail(f"    {original} -> {mapped}")
        if pack_result.files:
            logger.dry_run_notice(
                f"Would pack {len(pack_result.files)} file(s) -> {pack_result.bundle_path}"
            )
            for f in pack_result.files:
                logger.tree_item(f"  {f}")
        else:
            _warn_empty(logger, target, pack_result)
        return

    if pack_result.mapped_count:
        logger.progress(
            f"Mapped {pack_result.mapped_count} file(s){mapping_summary}"
        )
        for mapped, original in pack_result.path_mappings.items():
            logger.verbose_detail(f"    {original} -> {mapped}")

    if not pack_result.files:
        _warn_empty(logger, target, pack_result)
    else:
        logger.success(
            f"Packed {len(pack_result.files)} file(s) -> {pack_result.bundle_path}"
        )
        for f in pack_result.files:
            logger.verbose_detail(f"    {f}")
        if fmt == "plugin":
            logger.progress(
                "Plugin bundle ready -- contains plugin.json and "
                "plugin-native directories (agents/, skills/, commands/, ...). "
                "No APM-specific files included."
            )


def _render_marketplace_result(logger, report, dry_run, extra_warnings=None):
    """Render the marketplace producer's report (one-liner summary)."""
    if report is None:
        return
    for warn_msg in (extra_warnings or []):
        logger.warning(warn_msg)
    for warn_msg in report.warnings:
        logger.warning(warn_msg)
    if dry_run or report.dry_run:
        logger.dry_run_notice(
            f"Would write marketplace.json ({len(report.resolved)} package(s)) "
            f"-> {report.output_path}"
        )
        return
    logger.success(
        f"Built marketplace.json ({len(report.resolved)} package(s)) "
        f"-> {report.output_path}"
    )


@click.command(name="unpack", help="Extract an APM bundle into the current project")
@click.argument("bundle_path", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default=".",
    help="Target directory (default: current directory).",
)
@click.option("--skip-verify", is_flag=True, default=False, help="Skip bundle completeness check.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be unpacked without writing")
@click.option("--force", is_flag=True, default=False, help="Deploy despite critical hidden-character findings.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed unpacking information")
@click.pass_context
def unpack_cmd(ctx, bundle_path, output, skip_verify, dry_run, force, verbose):
    """Extract an APM bundle into the project."""
    logger = CommandLogger("unpack", verbose=verbose, dry_run=dry_run)
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
                logger.warning(
                    f"  {result.skipped_count} file(s) skipped (missing from bundle)"
                )
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


def _log_unpack_file_list(result, logger):
    """Log unpacked files grouped by dependency, using tree-style output."""
    if result.dependency_files:
        for dep_name, dep_files in result.dependency_files.items():
            logger.progress(f"  {dep_name}")
            for f in dep_files:
                logger.tree_item(f"    - {f}")
    else:
        for f in result.files:
            logger.tree_item(f"  - {f}")


def _mapping_summary(path_mappings):
    """Build a compact ': src/ -> dst/' suffix from path mappings, or empty string."""
    if not path_mappings:
        return ""
    # Derive source and destination prefixes from the first mapping entry
    src_sample = next(iter(path_mappings.values()))
    dst_sample = next(iter(path_mappings))
    src_root = src_sample.split("/")[0] + "/"
    dst_root = dst_sample.split("/")[0] + "/"
    return f": {src_root} -> {dst_root}"


def _warn_empty(logger, target, result):
    """Emit a contextual warning when the bundle has no files."""
    if target:
        # User explicitly asked for a target but got nothing
        # Check if there are source files under other prefixes
        if result.path_mappings or result.mapped_count:
            # Mapping was attempted but somehow produced nothing
            logger.warning(f"No files to pack for target '{target}'")
        else:
            logger.warning(f"No files to pack for target '{target}'")
            logger.verbose_detail(
                f"    Hint: use '--target all' to include all platforms"
            )
    else:
        logger.warning("No deployed files found -- empty bundle created")


def _log_bundle_meta(result, output_dir, logger):
    """Show bundle provenance and warn if target mismatches the project."""
    meta = result.pack_meta
    if not meta:
        return

    bundle_target = meta.get("target", "")
    dep_count = len(result.dependency_files) if result.dependency_files else 0
    file_count = len(result.files) if result.files else 0

    # Map internal canonical names to user-facing names for display
    _DISPLAY = {"vscode": "copilot", "agents": "copilot"}
    display_bundle = _DISPLAY.get(bundle_target, bundle_target)

    logger.progress(
        f"Bundle target: {display_bundle} "
        f"({dep_count} dep(s), {file_count} file(s))"
    )

    # Detect project target from output directory
    try:
        from ..core.target_detection import detect_target
        project_target, _reason = detect_target(output_dir.resolve())
    except Exception:
        return  # can't detect -- skip mismatch check

    display_project = _DISPLAY.get(project_target, project_target)

    # Normalize to canonical internal names for comparison
    _CANONICAL = {"copilot": "vscode", "agents": "vscode"}
    norm_bundle = _CANONICAL.get(bundle_target, bundle_target)
    norm_project = _CANONICAL.get(project_target, project_target)

    if norm_bundle == "all" or norm_project in ("all", "minimal"):
        return  # universal bundle or no strong project signal

    if norm_bundle != norm_project:
        logger.warning(
            f"Bundle target '{display_bundle}' differs from project "
            f"target '{display_project}'"
        )
        logger.verbose_detail(
            f"    To get a {display_project}-targeted bundle, "
            f"ask the publisher to run: apm pack --target {display_project}"
        )

