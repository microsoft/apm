"""Command-integrator module-level helpers extracted from _integrator.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from apm_cli.utils.paths import portable_relpath

from .._opts import IntegrateOpts
from ._transform import _write_gemini_command as _write_gemini_command_fn


def _collect_command_security_messages(scan_verdict, target: Path) -> list[tuple[str, str, str]]:
    """Return diagnostics payloads for post-transform security findings."""
    if scan_verdict is None:
        return []
    if scan_verdict.has_critical:
        return [
            (
                f"Critical hidden characters in {target.name}",
                (
                    f"{scan_verdict.critical_count} critical, "
                    f"{scan_verdict.warning_count} warning(s) -- "
                    f"run 'apm audit --file {target}' to inspect"
                ),
                "critical",
            )
        ]
    if scan_verdict.has_findings:
        return [
            (
                f"Hidden character warnings in {target.name}",
                (
                    f"{scan_verdict.warning_count} warning(s) -- "
                    f"run 'apm audit --file {target}' to inspect"
                ),
                "warning",
            )
        ]
    return []


def _emit_command_warnings(warnings: list[str], diagnostics, logger, package: str) -> None:
    """Emit non-security command warnings through the configured channel."""
    for warning in warnings:
        if diagnostics is not None:
            diagnostics.warn(message=warning, package=package)
        else:
            logger.warning(warning)


def _command_base_name(prompt_file: Path) -> str:
    """Return the command filename stem for one prompt file."""
    filename = prompt_file.name
    return filename[: -len(".prompt.md")] if filename.endswith(".prompt.md") else prompt_file.stem


@dataclass(frozen=True, slots=True)
class _CommandTargetContext:
    """Shared context for integrating one batch of command prompts."""

    target: Any
    mapping: Any
    commands_dir: Path
    package_info: Any
    project_root: Path
    managed_files: set[str] | None
    force: bool
    diagnostics: Any
    package_name: str


def _build_command_target_path(
    prompt_file: Path,
    commands_dir: Path,
    mapping,
    diagnostics,
    package_name: str,
) -> Path | None:
    """Validate and build the target path for one command file."""
    base_name = _command_base_name(prompt_file)
    try:
        validate_path_segments(base_name, context="command filename")
    except PathTraversalError as exc:
        if diagnostics is not None:
            diagnostics.warn(message=f"Rejected command filename: {exc}", package=package_name)
        return None
    target_path = commands_dir / f"{base_name}{mapping.extension}"
    try:
        ensure_path_within(target_path, commands_dir)
    except PathTraversalError as exc:
        if diagnostics is not None:
            diagnostics.warn(message=f"Rejected command target path: {exc}", package=package_name)
        return None
    return target_path


def _integrate_prompt_file(
    integrator,
    prompt_file: Path,
    ctx: _CommandTargetContext,
) -> tuple[int, int, int, int, Path | None, bool]:
    """Integrate one prompt file and return counter deltas."""
    target_path = _build_command_target_path(
        prompt_file,
        ctx.commands_dir,
        ctx.mapping,
        ctx.diagnostics,
        ctx.package_name,
    )
    if target_path is None:
        return (0, 1, 0, 0, None, False)
    rel_path = portable_relpath(target_path, ctx.project_root)
    if integrator.is_content_identical_to_source(target_path, prompt_file):
        return (0, 0, 1, 0, target_path, False)
    if integrator.check_collision(
        target_path,
        rel_path,
        ctx.managed_files,
        ctx.force,
        diagnostics=ctx.diagnostics,
    ):
        return (0, 1, 0, 0, None, False)
    if ctx.mapping.format_id == "gemini_command":
        _write_gemini_command_fn(prompt_file, target_path)
        return (1, 0, 0, 0, target_path, False)
    links_resolved, written, had_dropped = integrator.integrate_command(
        prompt_file,
        target_path,
        ctx.package_info,
        IntegrateOpts(diagnostics=ctx.diagnostics),
        target_name=ctx.target.name,
    )
    if not written:
        return (0, 1, 0, 0, None, had_dropped)
    return (1, 0, 0, links_resolved, target_path, had_dropped)


def _check_passthrough_notice(
    target_name: str,
    format_id: str,
    *,
    had_dropped_keys: bool,
    notified: set[str],
) -> bool:
    """Return True the first time target_name sees a passthrough deploy
    in which at least one file actually had dropped keys.

    Only fires for cursor-style targets that reuse the shared
    ``claude_command`` transformer (and would benefit from the
    cross-tool-compatibility explanation).  Returns False for
    targets that have their own dedicated writer (e.g. Gemini),
    and returns False on the happy path where no frontmatter keys
    were dropped (the notice would be pure noise then).
    """
    if not had_dropped_keys:
        return False
    if format_id != "claude_command" or target_name == "claude":
        return False
    if target_name in notified:
        return False
    notified.add(target_name)
    return True
