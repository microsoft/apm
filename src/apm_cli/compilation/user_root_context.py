"""User-scope root-context compilation engine.

Reads global (apply_to-less) instructions from ~/.apm/apm_modules and
writes each active target's user-scope root context file:
  claude  -> ~/.claude/CLAUDE.md  (or $CLAUDE_CONFIG_DIR/CLAUDE.md)
  codex   -> ~/.codex/AGENTS.md
  gemini  -> ~/.gemini/GEMINI.md
  copilot -> ~/.copilot/AGENTS.md
  vscode  -> ~/.copilot/AGENTS.md (same deploy root at user scope)
  cursor  -> ~/.cursor/AGENTS.md
  opencode -> ~/.config/opencode/AGENTS.md

Files are ONLY written when:
1. The target supports user scope (for_scope returns non-None)
2. The target has a recognised compile_family with a root-file mapping
3. Global instructions exist in the module tree
4. The existing file either does not exist OR carries the generated marker

Hand-authored files (no marker) are left untouched.
Claude instructions already delivered by equivalent native user rules are
omitted. Explicit cleanup can remove an unchanged, fully redundant Claude root.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging as _logging_module

    from ..integration.targets import TargetProfile
    from ..primitives.models import Instruction

# Root filename by compile_family.  Targets whose compile_family is not in
# this map do not produce a root file (e.g. family=None for agent-skills).
_ROOT_FILENAME: dict[str, str] = {
    "claude": "CLAUDE.md",
    "agents": "AGENTS.md",
    "vscode": "AGENTS.md",
    "gemini": "GEMINI.md",
}


@dataclass(frozen=True)
class UserRootCompileResult:
    """Result for one user-scope root context compilation target."""

    target: str
    path: Path | None
    status: str
    has_critical_security: bool = False
    warnings: tuple[str, ...] = ()


def _resolve_deploy_root(profile: TargetProfile) -> Path:
    """Return the absolute deploy root for a user-scoped TargetProfile.

    After for_scope(user_scope=True):
    * profile.resolved_deploy_root is set   -> use it directly
    * otherwise                             -> Path.home() / profile.root_dir
    """
    if profile.resolved_deploy_root is not None:
        return profile.resolved_deploy_root
    return Path.home() / profile.root_dir


def _finalize_build_id(content: str) -> str:
    """Replace the BUILD_ID_PLACEHOLDER sentinel with a 12-char content hash.

    The hash is computed over all lines EXCEPT the placeholder line so the
    result is deterministic (not self-referential).
    """
    from .constants import BUILD_ID_PLACEHOLDER

    lines = content.splitlines()
    try:
        idx = lines.index(BUILD_ID_PLACEHOLDER)
    except ValueError:
        return content

    hash_input_lines = [line for i, line in enumerate(lines) if i != idx]
    build_id = hashlib.sha256("\n".join(hash_input_lines).encode("utf-8")).hexdigest()[:12]
    lines[idx] = f"<!-- Build ID: {build_id} -->"
    return "\n".join(lines) + "\n"


def _generate_content(
    instructions: list[Instruction],
    *,
    preserve_scoped_sections: bool = False,
    base_dir: Path | None = None,
) -> str:
    """Generate root context content, retaining scoped sections when supported.

    Embeds the APM-generated marker and a deterministic Build ID so that
    subsequent runs can detect APM-owned files and apply overwrite protection.

    ASCII-only: no Unicode in the generated skeleton; instruction *content*
    is passed through as-is (callers are responsible for encoding checks).
    """
    from .constants import AGENTS_MD_GENERATED_MARKER, BUILD_ID_PLACEHOLDER

    sections: list[str] = [
        AGENTS_MD_GENERATED_MARKER,
        BUILD_ID_PLACEHOLDER,
        "",
    ]

    if preserve_scoped_sections:
        from .template_builder import render_instructions_block

        def emit(instruction: Instruction) -> list[str]:
            return [instruction.content.strip(), ""]

        sections.extend(
            render_instructions_block(
                instructions,
                base_dir=base_dir or Path.cwd(),
                emit_instruction=emit,
            )
        )
    else:
        for instruction in instructions:
            sections.append(instruction.content.strip())
            sections.append("")

    return _finalize_build_id("\n".join(sections))


def discover_global_instructions(
    source_root: Path,
    *,
    logger: _logging_module.Logger | None = None,
    include_scoped: bool = False,
) -> list[Instruction]:
    """Return global instructions and optionally scoped ones from ``apm_modules``.

    Returns an empty list when the ``apm_modules`` tree is absent or carries no
    matching instructions. Results are sorted by file path for determinism so
    callers (the compile engine and the install-time hint) agree on ordering.
    """
    from ..primitives.discovery import discover_primitives

    log = logger or logging.getLogger(__name__)

    apm_modules = source_root / "apm_modules"
    if not apm_modules.is_dir():
        log.debug(
            "user_root_context: apm_modules dir not found at %s -- no global instructions",
            apm_modules,
        )
        return []

    primitives = discover_primitives(str(apm_modules))
    return sorted(
        [instr for instr in primitives.instructions if include_scoped or not instr.apply_to],
        key=lambda instr: str(instr.file_path),
    )


def _handle_redundant_claude_root(
    target: str,
    path: Path,
    expected_content: str,
    *,
    clean: bool,
    dry_run: bool,
    warnings: tuple[str, ...] = (),
) -> UserRootCompileResult:
    """Retain or explicitly clean an unchanged root now covered by native rules.

    Global roots have no deployment hash ledger. Require the exact legacy
    output of the current instruction set, not merely a generated marker,
    before removal. Older or edited content is left for manual review.
    """
    from ..integration.cleanup import remove_stale_deployed_files
    from ..utils.diagnostics import DiagnosticCollector
    from .constants import AGENTS_MD_GENERATED_MARKER, has_generated_marker_header

    security_error = _validate_compiled_output_policy(target, path, expected_content, warnings)
    if security_error is not None:
        return security_error
    if path.is_symlink():
        return UserRootCompileResult(target, path, "skipped-symlink", warnings=warnings)
    if not path.exists():
        return UserRootCompileResult(target, path, "skipped-native-rules", warnings=warnings)
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return UserRootCompileResult(
            target, path, f"error:cannot read {path}: {exc}", warnings=warnings
        )
    if not has_generated_marker_header(existing, (AGENTS_MD_GENERATED_MARKER,)):
        return UserRootCompileResult(target, path, "skipped-hand-authored", warnings=warnings)
    if existing != expected_content:
        return UserRootCompileResult(target, path, "skipped-modified", warnings=warnings)
    if not clean:
        return UserRootCompileResult(target, path, "retained-redundant", warnings=warnings)
    if dry_run:
        return UserRootCompileResult(target, path, "would-remove", warnings=warnings)

    # Hash the expected generated output, not untrusted on-disk bytes. The
    # cleanup owner rechecks it and refuses a replacement symlink or user edit.
    expected_hash = "sha256:" + hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
    result = remove_stale_deployed_files(
        [path.name],
        path.parent,
        dep_key="global-claude-root",
        targets=[],
        diagnostics=DiagnosticCollector(),
        recorded_hashes={path.name: expected_hash},
        allowed_prefixes=(path.name,),
        allow_final_symlink=True,
        failed_path_retained=False,
    )
    if result.failed or result.skipped_unmanaged:
        return UserRootCompileResult(
            target,
            path,
            f"error:could not remove {path}; inspect its access and path, then retry",
            warnings=warnings,
        )
    if result.skipped_user_edit:
        return UserRootCompileResult(target, path, "skipped-modified", warnings=warnings)
    return UserRootCompileResult(target, path, "removed", warnings=warnings)


def _validate_compiled_output_policy(
    target: str,
    path: Path,
    content: str,
    warnings: tuple[str, ...],
) -> UserRootCompileResult | None:
    """Run the compiled-output security gate even when no file will be written."""
    from .output_writer import CompiledOutputPolicyError, CompiledOutputWriter

    try:
        CompiledOutputWriter().prepare({path: content})
    except CompiledOutputPolicyError:
        return UserRootCompileResult(
            target,
            path,
            "error:critical hidden characters in compiled output",
            has_critical_security=True,
            warnings=warnings,
        )
    return None


def compile_user_root_contexts(
    targets: Iterable[TargetProfile],
    source_root: Path,
    *,
    dry_run: bool = False,
    clean: bool = False,
    force_instructions: bool = False,
    logger: _logging_module.Logger | None = None,
) -> list[UserRootCompileResult]:
    """Compile user-scope root context files from global (apply_to-less) instructions.

    Iterates over *targets*, skipping any that:
    * do not support user scope (for_scope returns None)
    * have no recognised compile_family root-file mapping

    For each remaining target the function discovers global instructions from
    ``source_root / "apm_modules"``, generates content, and writes the root
    file -- unless the existing file is hand-authored (no marker).

    Args:
        targets: Iterable of TargetProfile instances to process.
        source_root: Root of the user's APM installation tree,
            e.g. ``Path.home() / ".apm"``.
        dry_run: When True, no files are written or directories created.
            The returned status values reflect what *would* happen.
        clean: Remove an unchanged Claude root fully covered by native rules.
            Other orphaned output and hand-authored or edited roots are retained.
        force_instructions: Include Claude instructions in the root file even
            when equivalent native rules are already present.
        logger: Optional logger.  Falls back to ``logging.getLogger(__name__)``.

    Returns:
        A list of UserRootCompileResult entries, one per target that was
        evaluated.  Each entry contains ``target``, ``path``, and ``status``.

        Status values:
        * ``"written"``              -- file was created or updated
        * ``"unchanged"``            -- file already matches generated content
        * ``"would-write"``          -- dry_run; file would have been written
        * ``"skipped-no-instructions"`` -- no global instructions found
        * ``"skipped-hand-authored"`` -- existing file has no APM marker
        * ``"skipped-native-rules"`` -- Claude rules already deliver all instructions
        * ``"retained-redundant"``   -- redundant generated root needs explicit cleanup
        * ``"skipped-modified"``    -- redundant root differs from expected output
        * ``"skipped-symlink"``     -- redundant root is a user-owned symlink
        * ``"removed"`` / ``"would-remove"`` -- explicit redundant-root cleanup
        * ``"error:<msg>"``          -- OS error during read or write
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within
    from .constants import AGENTS_MD_GENERATED_MARKER

    log = logger or logging.getLogger(__name__)

    results: list[UserRootCompileResult] = []
    pending: list[tuple[int, str, Path, str, tuple[str, ...]]] = []

    apm_modules = source_root / "apm_modules"
    if not apm_modules.is_dir():
        log.debug(
            "user_root_context: apm_modules dir not found at %s -- no root files written",
            apm_modules,
        )
        return results

    all_instructions = discover_global_instructions(source_root, logger=log, include_scoped=True)
    global_instructions = [instr for instr in all_instructions if not instr.apply_to]

    for target in targets:
        # Resolve to user scope; None == target does not support user scope
        scoped = target.for_scope(user_scope=True)
        if scoped is None:
            log.debug("user_root_context: %s does not support user scope -- skipping", target.name)
            continue

        family = scoped.compile_family
        if family not in _ROOT_FILENAME:
            log.debug(
                "user_root_context: %s compile_family=%r has no root-file mapping -- skipping",
                scoped.name,
                family,
            )
            continue

        preserve_scoped_sections = scoped.include_scoped_in_user_root_context
        target_instructions = all_instructions if preserve_scoped_sections else global_instructions
        if not target_instructions:
            log.debug(
                "user_root_context: no applicable instructions found in %s -- skipping %s",
                apm_modules,
                scoped.name,
            )
            results.append(UserRootCompileResult(scoped.name, None, "skipped-no-instructions"))
            continue

        deploy_root = _resolve_deploy_root(scoped)
        root_filename = _ROOT_FILENAME[family]
        lexical_output_path = deploy_root / root_filename
        try:
            output_path = ensure_path_within(lexical_output_path, deploy_root)
        except (PathTraversalError, RuntimeError) as exc:
            log.warning("user_root_context: unsafe output path for %s: %s", scoped.name, exc)
            results.append(
                UserRootCompileResult(scoped.name, deploy_root / root_filename, f"error:{exc}")
            )
            continue

        if family == "claude":
            from .instruction_dedup import uncovered_instructions

            native_warnings: list[str] = []
            if not force_instructions:
                target_instructions = uncovered_instructions(
                    "claude",
                    target_instructions,
                    deploy_root / "rules",
                    deploy_root,
                    native_warnings.append,
                )
            if not target_instructions:
                results.append(
                    _handle_redundant_claude_root(
                        scoped.name,
                        lexical_output_path,
                        _generate_content(global_instructions),
                        clean=clean,
                        dry_run=dry_run,
                        warnings=tuple(native_warnings),
                    )
                )
                continue
        else:
            native_warnings = []

        content = _generate_content(
            target_instructions,
            preserve_scoped_sections=preserve_scoped_sections,
            base_dir=source_root,
        )

        # -- overwrite protection --
        if output_path.exists():
            try:
                existing = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("user_root_context: cannot read %s: %s", output_path, exc)
                results.append(UserRootCompileResult(scoped.name, output_path, f"error:{exc}"))
                continue

            if not existing.lstrip().startswith(AGENTS_MD_GENERATED_MARKER):
                log.info(
                    "user_root_context: %s is hand-authored (no APM marker) -- not overwriting",
                    output_path,
                )
                results.append(
                    UserRootCompileResult(scoped.name, output_path, "skipped-hand-authored")
                )
                continue

            if existing == content:
                log.debug("user_root_context: %s is unchanged", output_path)
                results.append(UserRootCompileResult(scoped.name, output_path, "unchanged"))
                continue

        if dry_run:
            log.debug("user_root_context: [dry-run] would write %s", output_path)
            results.append(
                UserRootCompileResult(
                    scoped.name,
                    output_path,
                    "would-write",
                    warnings=tuple(native_warnings),
                )
            )
            continue

        index = len(results)
        warnings = tuple(native_warnings)
        results.append(
            UserRootCompileResult(scoped.name, output_path, "pending", warnings=warnings)
        )
        pending.append((index, scoped.name, output_path, content, warnings))

    if pending:
        from .output_writer import CompiledOutputPolicyError, CompiledOutputWriter

        try:
            verdict = CompiledOutputWriter().write_many(
                {path: content for _, _, path, content, _ in pending}
            )
        except CompiledOutputPolicyError:
            for index, name, path, _, warnings in pending:
                results[index] = UserRootCompileResult(
                    name,
                    path,
                    "error:critical hidden characters in compiled output",
                    has_critical_security=True,
                    warnings=warnings,
                )
        except OSError as exc:
            log.warning("user_root_context: failed to write output batch: %s", exc)
            for index, name, path, _, warnings in pending:
                results[index] = UserRootCompileResult(
                    name, path, f"error:{exc}", warnings=warnings
                )
        else:
            for index, name, path, _, warnings in pending:
                findings = verdict.findings_by_file.get(str(path), [])
                if findings:
                    log.warning(
                        "user_root_context: %s contains %s hidden character(s) "
                        "-- run 'apm audit --file %s' to inspect",
                        path,
                        len(findings),
                        path,
                    )
                log.debug("user_root_context: wrote %s", path)
                results[index] = UserRootCompileResult(name, path, "written", warnings=warnings)

    return results
