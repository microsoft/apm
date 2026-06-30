"""Click commands for ``apm pack`` and ``apm unpack``."""

import json as json_mod
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
from ..utils.console import set_console_stderr

MARKETPLACE_DOCS_URL = (
    "https://microsoft.github.io/apm/producer/publish-to-a-marketplace/#consume-from-any-assistant"
)

_PACK_HELP = """\
Pack distributable artifacts from your APM project.

Reads apm.yml to decide what to produce:

  dependencies: block  ->  bundle (directory or archive; see --archive and --archive-format)
  marketplace: block   ->  selected marketplace artifacts
  target: / targets:   ->  ecosystem-specific plugin.json (claude/copilot)
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


def _emit_json_error_or_raise(ctx, json_output: bool, code: str, message: str):
    """Emit a JSON error envelope to stdout or raise ClickException."""
    if json_output:
        from ..marketplace.builder import BuildReport

        click.echo(
            json_mod.dumps(
                BuildReport.failure_to_json_dict(errors=[{"code": code, "message": message}])
            )
        )
        ctx.exit(1)
    else:
        raise click.ClickException(message)


def _parse_path_overrides(
    marketplace_path_overrides: "tuple[str, ...]",
    ctx,
    json_output: bool,
) -> "dict[str, str] | None":
    """Parse --marketplace-path KEY=VALUE pairs.

    Returns a dict mapping format name -> path, or ``None`` on the first
    validation error (after emitting the error via *ctx*).
    """
    from ..marketplace.output_profiles import known_output_names
    from ..utils.path_security import validate_path_segments

    path_overrides: dict[str, str] = {}
    for override in marketplace_path_overrides:
        if "=" not in override:
            msg = f"--marketplace-path must be FORMAT=PATH, got: {override!r}"
            _emit_json_error_or_raise(ctx, json_output, "cli_error", msg)
            return None
        fmt_name, path_val = override.split("=", 1)
        fmt_name = fmt_name.strip()
        path_val = path_val.strip()
        if fmt_name not in known_output_names():
            msg = (
                f"Unknown marketplace format '{fmt_name}' in --marketplace-path. "
                f"Known formats: {', '.join(sorted(known_output_names()))}"
            )
            _emit_json_error_or_raise(ctx, json_output, "unknown_format", msg)
            return None
        # Security: validate path to prevent traversal attacks
        try:
            validate_path_segments(path_val, context="--marketplace-path", allow_current_dir=True)
        except Exception as exc:
            _emit_json_error_or_raise(ctx, json_output, "path_error", str(exc))
            return None
        path_overrides[fmt_name] = path_val
    return path_overrides


def _parse_marketplace_filter(
    marketplace_filter: "str | None",
    ctx,
    json_output: bool,
) -> "tuple[str, ...] | None":
    """Parse the --marketplace filter value.

    Returns:
      - ``None``           -- build all configured outputs
      - empty ``tuple``    -- skip marketplace entirely (``--marketplace none``)
      - non-empty tuple    -- build only the named formats
      - ``None`` on validation error (after emitting the error via *ctx*)
    """
    from ..marketplace.output_profiles import known_output_names

    if marketplace_filter is None:
        return None
    if marketplace_filter.strip().lower() == "none":
        return ()
    if marketplace_filter.strip().lower() == "all":
        return None  # all configured
    requested = [f.strip() for f in marketplace_filter.split(",") if f.strip()]
    known = known_output_names()
    for r in requested:
        if r not in known:
            msg = (
                f"Unknown marketplace format '{r}' in --marketplace. "
                f"Known formats: {', '.join(sorted(known))}"
            )
            _emit_json_error_or_raise(ctx, json_output, "unknown_format", msg)
            return None
    return tuple(requested)


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
    help=(
        "Produce a .zip archive instead of a directory (previous default: .tar.gz; "
        "use --archive-format tar.gz for legacy CI pipelines)."
    ),
)
@click.option(
    "--archive-format",
    "archive_format",
    type=click.Choice(["zip", "tar.gz"]),
    default="zip",
    show_default=True,
    help=(
        "Archive format when --archive is set. "
        "'zip' (default) is Claude Code and plugin-host compatible and matches apm publish output. "
        "'tar.gz' is typically smaller for text-heavy bundles and preserves the previous "
        "default for CI pipelines that rely on it."
    ),
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
    "--force",
    is_flag=True,
    default=False,
    help="Allow overwriting on collision: last-writer-wins in plugin bundles; "
    "overwrites any existing plugin.json at the generated manifest path.",
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
def pack_cmd(  # noqa: PLR0913 -- Click handler, one param per CLI option
    ctx,
    fmt,
    target,
    archive,
    archive_format,
    output,
    dry_run,
    force,
    verbose,
    offline,
    include_prerelease,
    marketplace_filter,
    marketplace_path_overrides,
    json_output,
    legacy_skill_paths,
    check_versions,
    check_clean,
):
    """Pack APM artifacts: bundle and/or marketplace.json."""
    # -- Stream discipline: under --json, route ALL output to stderr --
    if json_output:
        set_console_stderr(True)

    logger = CommandLogger("pack", verbose=verbose, dry_run=dry_run)

    # Error when --archive-format is explicitly set but --archive is not.
    if (
        not archive
        and ctx.get_parameter_source("archive_format") is click.core.ParameterSource.COMMANDLINE
    ):
        raise click.UsageError(
            f"--archive-format has no effect without --archive;"
            f" add --archive to produce a .{archive_format} archive."
        )

    # -- Parse --marketplace-path overrides --
    path_overrides_result = _parse_path_overrides(marketplace_path_overrides, ctx, json_output)
    if path_overrides_result is None:
        return
    path_overrides = path_overrides_result

    # -- Parse --marketplace filter --
    marketplace_formats = _parse_marketplace_filter(marketplace_filter, ctx, json_output)
    # _parse_marketplace_filter raises/exits on error via _emit_json_error_or_raise
    project_root = Path(".").resolve()
    # Authoring-path nudge (#1777): warn when the author's own package declares
    # no license. Suppressed under --json (machine output). Never blocks pack.
    if not json_output:
        from ..export.authoring import warn_if_license_undeclared

        warn_if_license_undeclared(project_root / "apm.yml", logger.warning)
    # Issue #1207 D1: when --target is not given, detect the project's
    # actual target so the embedded ``pack.target`` reflects what was
    # tested rather than a hardcoded "copilot".  ``pack.target`` is now
    # informational metadata only -- consumer-side install resolves the
    # deploy target from the consumer project's context, not from the
    # bundle.
    if target is None:
        from ..core.target_detection import detect_target

        try:
            detected, _reason = detect_target(project_root)
            effective_target = detected if detected else None
        except Exception:
            effective_target = None
    else:
        logger.warning(
            "--target is deprecated and will be removed in a future release. "
            "Bundles are target-agnostic; the value is recorded as informational "
            "pack.target metadata only and is ignored by 'apm install'."
        )
        effective_target = target
    options = BuildOptions(
        project_root=project_root,
        apm_yml_path=project_root / "apm.yml",
        bundle_format=fmt,
        bundle_target=effective_target,
        bundle_archive=archive,
        bundle_archive_format=archive_format,
        bundle_output=Path(output),
        bundle_force=force,
        marketplace_offline=offline,
        marketplace_include_prerelease=include_prerelease,
        marketplace_formats=marketplace_formats,
        marketplace_path_overrides=path_overrides if path_overrides else None,
        dry_run=dry_run,
        verbose=verbose,
    )

    try:
        result = BuildOrchestrator().run(options, logger=logger)
    except BuildError as exc:
        _emit_json_error_or_raise(ctx, json_output, "build_error", str(exc))
        return

    # -- Release gates (--check-versions / --check-clean) --
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

        # Try to load the marketplace config; if absent, skip both gates with [i].
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
                # Use a builder with dry_run=True so the gate itself
                # never mutates the working tree.
                mkt_opts = MktBuildOptions(
                    dry_run=True,
                    offline=options.marketplace_offline,
                    include_prerelease=options.marketplace_include_prerelease,
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
                                _emit_drift_recipe(logger, out.path)
                            else:
                                count = len(out.differences)
                                logger.info(f"    {out.path}  [drift: {count} differences]")
                                for line in render_diff_lines(out):
                                    logger.info(line)
                                _emit_drift_recipe(logger, out.path)
                    for msg in d_report.error_messages():
                        gate_errors.append({"code": "marketplace_drift", "message": msg})

    # -- JSON output mode: consistent envelope --
    if json_output:
        envelope = {
            "ok": True,
            "dry_run": dry_run,
            "warnings": [],
            "errors": [],
            "marketplace": {"outputs": []},
            "bundle": None,
            "plugin_manifests": {"written": [], "skipped": [], "dry_run": []},
            "version_alignment": version_alignment_payload,
            "drift": drift_payload,
        }
        for sub in result.producer_results:
            if sub.kind is OutputKind.MARKETPLACE and sub.payload is not None:
                payload = sub.payload.to_json_dict()
                envelope["warnings"] = payload.get("warnings", [])
                envelope["marketplace"] = payload.get("marketplace", {"outputs": []})
            elif sub.kind is OutputKind.PLUGIN_MANIFEST and isinstance(sub.payload, dict):
                envelope["plugin_manifests"] = sub.payload
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
            _render_bundle_result(
                logger,
                sub.payload,
                fmt,
                target,
                dry_run,
                show_zip_migration_notice=(
                    archive
                    and archive_format == "zip"
                    and ctx.get_parameter_source("archive_format")
                    is not click.core.ParameterSource.COMMANDLINE
                ),
            )
        elif sub.kind is OutputKind.MARKETPLACE:
            _render_marketplace_result(logger, sub.payload, dry_run, sub.warnings, sub.outputs)

    # Gate exit codes (after non-JSON rendering above): 3 wins over 4.
    if version_gate_failed:
        ctx.exit(3)
    if drift_gate_failed:
        ctx.exit(4)


def _emit_drift_recipe(logger, out_path: str) -> None:
    """Emit the canonical recovery recipe when marketplace.json drift is detected.

    Teaches producers the amend+force-with-lease pattern so they can fix the
    drift without a noisy follow-up commit.
    """
    logger.info("")
    logger.info("    To recover cleanly (fold into the current commit):")
    logger.info("")
    logger.info("      apm pack                       # regenerate locally")
    logger.info(f"      git add -- {out_path}")
    logger.info("      git commit --amend --no-edit   # fold into the current commit")
    logger.info("      git push --force-with-lease    # safe re-push")
    logger.info("")
    logger.info("    Or as a follow-up commit:")
    logger.info("")
    logger.info(f"      apm pack && git add -- {out_path}")
    logger.info("      git commit -m 'chore(marketplace): regen'")
    logger.info("")
    logger.info("    Why this exists: marketplace.json is checked in (lockfile pattern)")
    logger.info("    so consumers can resolve packages without running 'apm pack'. CI")
    logger.info("    enforces that the checked-in copy matches the apm.yml source of truth.")


def _bundle_size_suffix(bundle_path) -> str:
    """Return a small size suffix for existing archive files."""
    if not bundle_path:
        return ""
    path = Path(bundle_path)
    if not path.is_file():
        return ""
    size = path.stat().st_size
    if size < 1024:
        return f" ({size} bytes)"
    if size < 1024 * 1024:
        return f" ({size / 1024:.1f} KiB)"
    return f" ({size / (1024 * 1024):.1f} MiB)"


def _render_bundle_result(
    logger,
    pack_result,
    fmt,
    target,
    dry_run,
    *,
    show_zip_migration_notice: bool = False,
):
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
        logger.progress(f"Mapped {pack_result.mapped_count} file(s){mapping_summary}")
        for mapped, original in pack_result.path_mappings.items():
            logger.verbose_detail(f"    {original} -> {mapped}")

    if not pack_result.files:
        _warn_empty(logger, target, pack_result)
    else:
        size_suffix = _bundle_size_suffix(pack_result.bundle_path)
        logger.success(
            f"Packed {len(pack_result.files)} file(s) -> {pack_result.bundle_path}{size_suffix}"
        )
        for f in pack_result.files:
            logger.verbose_detail(f"    {f}")
        if show_zip_migration_notice and str(pack_result.bundle_path).endswith(".zip"):
            logger.info(
                "Note: --archive now produces .zip by default. "
                "Use --archive-format tar.gz to restore the previous format for legacy pipelines."
            )
            logger.verbose_detail(
                "    Tip: use --archive-format tar.gz for smaller archives on text-heavy bundles."
            )
        if fmt == "plugin":
            logger.progress(
                "Plugin bundle ready -- contains plugin.json plus "
                "plugin-native directories (agents/, skills/, commands/, ...) "
                "and an embedded apm.lock.yaml for install-time integrity "
                "verification."
            )
        # Issue #1207: target-agnostic bundles install into any consumer
        # project.  Print a copy-pasteable share line so packing creates
        # the social hand-off naturally.
        if pack_result.bundle_path:
            logger.info(f"Share with: apm install {pack_result.bundle_path}")


def _render_marketplace_result(logger, report, dry_run, extra_warnings=None, outputs=None):
    """Render the marketplace producer's report.

    Emits per-output success/dry-run lines first, then a vendor-neutral
    catalog of artifact paths plus a single docs pointer. The catalog
    block is suppressed in dry-run mode (no files were actually written).
    """
    seen_warnings = set()
    for warn_msg in extra_warnings or []:
        seen_warnings.add(warn_msg)
        logger.warning(warn_msg)
    for warn_msg in getattr(report, "warnings", ()) or ():
        if warn_msg in seen_warnings:
            continue
        seen_warnings.add(warn_msg)
        logger.warning(warn_msg)

    output_reports = tuple(getattr(report, "outputs", ()) or ())
    written: list[tuple[str | None, Path]] = []
    if not output_reports:
        package_count = len(getattr(report, "resolved", ()) or ()) if report is not None else None
        for output in outputs or []:
            message = f"marketplace.json -> {output}"
            if package_count is not None:
                message = f"marketplace.json ({package_count} package(s)) -> {output}"
            if dry_run:
                logger.dry_run_notice(f"Would write {message}")
            else:
                logger.success(f"Built {message}")
                written.append((None, Path(output)))
    else:
        for output_report in output_reports:
            message = (
                f"marketplace.json [{output_report.profile}] "
                f"({len(output_report.resolved)} package(s)) -> {output_report.output_path}"
            )
            if dry_run or output_report.dry_run:
                logger.dry_run_notice(f"Would write {message}")
            else:
                logger.success(f"Built {message}")
                written.append((output_report.profile, Path(output_report.output_path)))

    if written and not dry_run:
        _render_marketplace_catalog(logger, written)


def _render_marketplace_catalog(logger, written: list[tuple[str | None, Path]]) -> None:
    """Append a vendor-neutral catalog of marketplace artifacts.

    Renders one ``[i]`` info header, one ``[i]`` two-column row per
    artifact, and a single ``[i]`` pointer to the docs anchor that
    enumerates per-assistant install commands. Never names a vendor CLI
    surface inline -- APM is vendor-agnostic and the install command
    varies by AI assistant.
    """
    info = getattr(logger, "info", None)
    if info is None:
        return

    info("Marketplace artifacts ready:")
    if any(profile for profile, _ in written):
        label_width = max(len(profile or "") for profile, _ in written)
        for profile, path in written:
            tag = (profile or "").ljust(label_width)
            info(f"  [{tag}] {path}")
    else:
        for _, path in written:
            info(f"  {path}")

    info("How consumers install from this marketplace varies by AI assistant.")
    info(f"See: {MARKETPLACE_DOCS_URL}")


@click.command(
    name="unpack",
    help=(
        "[Deprecated] Extract an APM bundle into the current project. "
        "Use 'apm install <bundle-path>' instead -- this command will be removed in a future release."
    ),
    short_help="[Deprecated] Use 'apm install <bundle-path>' instead.",
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
        "'apm unpack' is deprecated and will be removed in a future release. "
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

        if result.canvas_blocked > 0:
            from apm_cli.core.experimental import is_enabled

            if not is_enabled("canvas"):
                logger.warning(
                    f"Blocked {result.canvas_blocked} canvas extension file(s): canvas "
                    "extensions are an experimental feature and are disabled. Enable "
                    "them with 'apm experimental enable canvas'."
                )

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
                "    Hint: check that apm.lock.yaml has deployed_files entries (run apm install first)"
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

    logger.progress(f"Bundle target: {display_bundle} ({dep_count} dep(s), {file_count} file(s))")

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
            f"Bundle target '{display_bundle}' differs from project target '{display_project}'"
        )
        logger.verbose_detail(
            f"    To get a {display_project}-targeted bundle, "
            f"ask the publisher to run: apm pack --target {display_project}"
        )
