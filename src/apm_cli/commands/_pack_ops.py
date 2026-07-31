"""Release-gate logic extracted from pack_cmd to reduce complexity.

``_run_release_gates`` handles --check-versions and --check-clean.
``_emit_drift_recipe`` is the recovery-recipe printer used by drift reporting.

These functions do not reference any names patched by tests on
``apm_cli.commands.pack`` (BuildOrchestrator is used only in the caller),
so no late-import routing is needed here.
"""

from __future__ import annotations

from pathlib import Path


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


def _run_release_gates(
    ctx,
    options,
    check_versions: bool,
    check_clean: bool,
    json_output: bool,
    logger,
    project_root: Path,
) -> tuple[bool, bool, dict | None, dict | None, list]:
    """Run --check-versions and --check-clean release gates.

    Returns ``(version_gate_failed, drift_gate_failed,
                version_alignment_payload, drift_payload, gate_errors)``.

    When the marketplace config is absent both gates are skipped with an
    info message and the function returns all-clean values.
    """
    from ..marketplace.builder import BuildOptions as MktBuildOptions
    from ..marketplace.builder import MarketplaceBuilder
    from ..marketplace.drift_check import check_marketplace_drift, render_diff_lines
    from ..marketplace.migration import ConfigSource, detect_config_source
    from ..marketplace.version_check import check_version_alignment
    from ..marketplace.yml_schema import MarketplaceYmlError

    # Inline helper to keep this function self-contained
    from .pack import _emit_json_error_or_raise

    version_alignment_payload: dict | None = None
    drift_payload: dict | None = None
    gate_errors: list[dict] = []
    version_gate_failed = False
    drift_gate_failed = False

    gate_config = None
    try:
        source = detect_config_source(project_root)
        if source != ConfigSource.NONE:
            from ..marketplace.migration import load_marketplace_config

            gate_config = load_marketplace_config(project_root)
    except MarketplaceYmlError as exc:
        _emit_json_error_or_raise(ctx, json_output, "build_error", str(exc))
        return (False, False, None, None, [])

    if gate_config is None:
        if check_versions:
            logger.info("Version alignment check skipped: no marketplace block; nothing to check.")
        if check_clean:
            logger.info("Marketplace drift check skipped: no marketplace block; nothing to check.")
        return (False, False, None, None, [])

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

    return (
        version_gate_failed,
        drift_gate_failed,
        version_alignment_payload,
        drift_payload,
        gate_errors,
    )


# -- Unpack / pack render helpers (moved from pack.py) --------------


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
