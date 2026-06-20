"""Mixin: emit methods for CLAUDE.md, GEMINI.md, and Copilot root instructions.

Extracted from agents_compiler.AgentsCompiler to stay under the 800-line
guardrail (Strangler Stage 2 / issue #1078).

Rule B routing
--------------
Three module-level names in agents_compiler are patched by tests:
  ``resolve_markdown_links``, ``discover_primitives``, ``_logger``.
Any moved method that references them does so via a **function-level** late
import so the mock installed by the test suite is picked up at call time:

    from apm_cli.compilation import agents_compiler as _ac
    _ac.resolve_markdown_links(...)
    _ac._logger.debug(...)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..primitives.models import PrimitiveCollection
from ..utils.paths import portable_relpath
from ..version import get_version
from .claude_formatter import CLAUDE_HEADER
from .constants import BUILD_ID_PLACEHOLDER

if TYPE_CHECKING:
    from .agents_compiler import CompilationConfig, CompilationResult


class StaleClaudeDetection(NamedTuple):
    """Result of _detect_stale_claude_md: describes a candidate stale CLAUDE.md.

    Attributes:
        path       -- absolute Path to the candidate CLAUDE.md
        rel        -- portable relative path string (for user-facing messages)
        exists     -- True if the file is present on disk
        has_marker -- True if the file contains CLAUDE_HEADER (APM-generated)
        read_error -- non-None error string if the file exists but could not be read
    """

    path: Path
    rel: str
    exists: bool
    has_marker: bool
    read_error: str | None

    @property
    def is_apm_managed(self) -> bool:
        """Return True when the file is readable and carries APM's marker."""
        return self.exists and self.has_marker and self.read_error is None


def _hand_authored_claude_skip_message(
    rel: str, *, dry_run: bool = False, preview: bool = False
) -> str:
    """Build consistent skip guidance for hand-authored CLAUDE.md files."""
    prefix = "[dry-run] would skip removal" if preview or dry_run else "Skipped removal"
    return (
        f"{prefix} of {rel}: hand-authored file will not be deleted."
        " Delete or rename it manually if duplicate context is unwanted."
    )


class _AgentsEmitMixin:
    """Mixin: CLAUDE.md / GEMINI.md / copilot-instructions.md emit methods."""

    # ------------------------------------------------------------------ #
    # Stale CLAUDE.md detection                                            #
    # ------------------------------------------------------------------ #

    def _detect_stale_claude_md(self) -> StaleClaudeDetection:
        """Detect whether a stale APM-generated CLAUDE.md exists at the project root.

        Returns a StaleClaudeDetection with fields:
            path       -- absolute Path to the candidate CLAUDE.md
            rel        -- portable relative path string (for user-facing messages)
            exists     -- True if the file is present on disk
            has_marker -- True if the file contains CLAUDE_HEADER (APM-generated)
            read_error -- non-None error string if the file exists but could not be read

        Both the dry-run preview block and the live removal block share this
        helper so the read + marker-detect + rel computation lives in one place.
        """
        # Rule B: ensure_path_within is patched at agents_compiler.ensure_path_within in tests.
        from apm_cli.compilation import agents_compiler as _ac_mod

        root_claude_md = self.base_dir / "CLAUDE.md"
        rel = portable_relpath(root_claude_md, self.base_dir)
        if not root_claude_md.exists():
            return StaleClaudeDetection(root_claude_md, rel, False, False, None)
        try:
            _ac_mod.ensure_path_within(root_claude_md, self.base_dir)
            with root_claude_md.open("rb") as fh:
                # CLAUDE_HEADER is emitted on the first line; 4 KiB is ample.
                content = fh.read(4096).decode("utf-8")  # strict
        except (OSError, _ac_mod.PathTraversalError, UnicodeDecodeError) as exc:
            return StaleClaudeDetection(
                root_claude_md, rel, True, False, f"Could not read {rel}: {exc!s}"
            )
        return StaleClaudeDetection(root_claude_md, rel, True, CLAUDE_HEADER in content, None)

    # ------------------------------------------------------------------ #
    # CLAUDE.md compilation                                                #
    # ------------------------------------------------------------------ #

    def _compile_claude_md(
        self,
        config: CompilationConfig,
        primitives: PrimitiveCollection,
    ) -> CompilationResult:
        """Compile CLAUDE.md files (Claude Code target)."""
        from apm_cli.compilation.agents_compiler import CompilationResult

        errors = self.validate_primitives(primitives)
        self.errors.extend(errors)

        # Create Claude formatter
        from .claude_formatter import ClaudeFormatter

        claude_formatter = ClaudeFormatter(str(self.base_dir), source_dir=str(self.source_dir))

        # Honor compilation.strategy=single-file (and the --single-agents flag)
        # by collapsing all instructions into a single root CLAUDE.md, mirroring
        # the gate in _compile_agents_md. Without this, single-file mode is
        # silently ignored for the Claude target and per-subdirectory CLAUDE.md
        # files are emitted via the distributed placement path (issue #1445).
        #
        # DistributedAgentsCompiler is only constructed on the distributed
        # branch -- single-file mode does not use its placement analysis and
        # the later display block guards on `distributed_compiler is not None`.
        distributed_compiler = None
        if config.strategy != "distributed" or config.single_agents:
            placement_map = {self.base_dir: list(primitives.instructions)}
        else:
            from .distributed_compiler import DistributedAgentsCompiler

            distributed_compiler = DistributedAgentsCompiler(
                str(self.base_dir),
                exclude_patterns=config.exclude,
                source_dir=str(self.source_dir),
            )
            # Analyze directory structure and determine placement
            directory_map = distributed_compiler.analyze_directory_structure(
                primitives.instructions
            )
            placement_map = distributed_compiler.determine_agents_placement(
                primitives.instructions,
                directory_map,
                min_instructions=config.min_instructions_per_file,
                debug=config.debug,
            )

        # Skip instructions in CLAUDE.md when they are already deployed to
        # .claude/rules/ by `apm install` (avoids duplicate context in Claude Code).
        # --no-dedup / --force-instructions lets users opt out of this behaviour.
        from .agents_compiler import _detect_deployed_instructions

        if config.no_dedup:
            skip_instructions = False
            self._log(
                "progress",
                "Including instructions in CLAUDE.md (--no-dedup overrides deduplication)",
                symbol="info",
            )
        else:
            skip_instructions = _detect_deployed_instructions(
                self.base_dir / ".claude" / "rules",
                self.base_dir,
                lambda msg: self._log("warning", msg),
            )
            if skip_instructions:
                self._log(
                    "progress",
                    "Instructions already in .claude/rules/ -- omitting from CLAUDE.md"
                    " to avoid duplicate context",
                    symbol="info",
                )

        # Format CLAUDE.md files
        claude_config = {
            "source_attribution": config.source_attribution,
            "debug": config.debug,
            "skip_instructions": skip_instructions,
        }
        claude_result = claude_formatter.format_distributed(
            primitives, placement_map, claude_config
        )

        # NOTE: Claude commands are now generated at install time via CommandIntegrator,
        # not at compile time. This keeps behavior consistent with VSCode prompt integration.

        # Merge warnings and errors (no command result anymore)
        all_warnings = self.warnings + claude_result.warnings
        all_errors = self.errors + claude_result.errors

        # would_emit_no_claude_md is True when the formatter produced no CLAUDE.md
        # files because skip_instructions fired (all content already in .claude/rules/).
        # Used symmetrically in the dry-run preview block and the live-removal block so
        # both paths share a single, precise emptiness signal.
        would_emit_no_claude_md = len(claude_result.content_map) == 0 and skip_instructions

        # Handle dry-run mode
        if config.dry_run:
            # Generate preview summary
            count = len(claude_result.placements)
            preview_lines = [
                f"CLAUDE.md Preview: Would generate {count} {'file' if count == 1 else 'files'}"
            ]
            # Surface the deduplication skip so dry-run is self-explanatory
            # for scripted consumers (otherwise "Would generate 0 files"
            # looks like a no-op or a bug). The same skip appears in the
            # non-dry-run path via the dedicated INFO log line.
            if skip_instructions:
                preview_lines.append(
                    "  (instructions section skipped: .claude/rules/ already "
                    "populated -- avoids duplicate content in Claude Code's "
                    "context window)"
                )
            for claude_path in claude_result.content_map.keys():  # noqa: SIM118
                rel_path = portable_relpath(claude_path, self.base_dir)
                preview_lines.append(f"  {rel_path}")
            # Preview stale CLAUDE.md removal so --dry-run --clean is self-explanatory.
            # Read the file here (safe in dry-run: we never mutate it) to check for
            # the APM marker, mirroring the Copilot root-instructions convention.
            # Only show the preview when no CLAUDE.md would be written -- a project
            # WITH a constitution emits a CLAUDE.md and should NOT show a removal preview.
            if would_emit_no_claude_md and config.clean_orphaned:
                det = self._detect_stale_claude_md()
                if det.exists:
                    if det.read_error is not None:
                        all_warnings.append(det.read_error)
                        self._log("warning", det.read_error)
                    elif det.is_apm_managed:
                        removal_msg = (
                            f"[dry-run] would remove stale {det.rel} -- instructions now"
                            " live in .claude/rules/"
                        )
                        preview_lines.append(f"  {removal_msg}")
                        self._log("progress", removal_msg, symbol="info")
                    else:
                        hand_authored_preview = _hand_authored_claude_skip_message(
                            det.rel, preview=True
                        )
                        preview_lines.append(f"  {hand_authored_preview}")
                        all_warnings.append(
                            _hand_authored_claude_skip_message(det.rel, dry_run=True)
                        )
                        self._log("progress", hand_authored_preview, symbol="info")

            return CompilationResult(
                success=len(all_errors) == 0,
                output_path="Preview mode - CLAUDE.md",
                content="\n".join(preview_lines),
                warnings=all_warnings,
                errors=all_errors,
                stats=claude_result.stats,
            )

        # Write CLAUDE.md files
        files_written = 0
        critical_security_found = False
        # Rule B: _logger is patched at agents_compiler._logger in tests
        from apm_cli.compilation import agents_compiler as _ac

        from ..security.gate import WARN_POLICY, SecurityGate
        from .output_writer import CompiledOutputWriter

        writer = CompiledOutputWriter()
        for claude_path, content in claude_result.content_map.items():
            try:
                # Handle constitution injection if enabled
                final_content = content
                if config.with_constitution:
                    try:
                        from .injector import ConstitutionInjector

                        injector = ConstitutionInjector(str(claude_path.parent))
                        final_content, _, _ = injector.inject(
                            content, with_constitution=True, output_path=claude_path
                        )
                    except Exception as exc:
                        _ac._logger.debug(
                            "Constitution injection failed for %s: %s", claude_path, exc
                        )

                # Defense-in-depth: scan compiled output before writing
                verdict = SecurityGate.scan_text(
                    final_content, str(claude_path), policy=WARN_POLICY
                )
                actionable = verdict.critical_count + verdict.warning_count
                if actionable:
                    if verdict.has_critical:
                        critical_security_found = True
                    all_warnings.append(
                        f"CLAUDE.md contains {actionable} hidden character(s) "
                        f"-- run 'apm audit --file {claude_path}' to inspect"
                    )

                writer.write(claude_path, final_content)
                files_written += 1
            except OSError as e:
                all_errors.append(f"Failed to write {claude_path}: {e!s}")

        # Update stats
        stats = claude_result.stats.copy()
        stats["claude_files_written"] = files_written

        if would_emit_no_claude_md:
            self._log(
                "progress",
                "CLAUDE.md not generated -- Claude Code reads .claude/rules/ directly,"
                " no further action needed",
                symbol="info",
            )
            # Remove a stale APM-generated CLAUDE.md when --clean is set.
            # A hand-authored file (no CLAUDE_HEADER marker) is never deleted;
            # a warning is emitted instead to match the Copilot-root convention.
            # Dry-run mode returns earlier in this method, so this live block is
            # only reached when NOT in dry-run; no config.dry_run guard is needed.
            # Gate on clean_orphaned so plain `apm compile` (no --clean) does NO
            # extra disk I/O and emits NO stale-file warnings (non-destructive by design).
            if config.clean_orphaned:
                det = self._detect_stale_claude_md()
                if det.exists:
                    if det.read_error is not None:
                        all_warnings.append(det.read_error)
                        self._log("warning", det.read_error)
                    elif det.is_apm_managed:
                        try:
                            _ac.ensure_path_within(det.path, self.base_dir)
                            det.path.unlink()  # safe: containment + APM marker confirmed above
                            self._log(
                                "success",
                                f"Removed stale {det.rel} -- instructions now live in .claude/rules/",
                            )
                        except (OSError, _ac.PathTraversalError) as exc:
                            warning = f"Could not remove {det.rel}: {exc!s}"
                            all_warnings.append(warning)
                            self._log("warning", warning)
                    else:
                        warning = _hand_authored_claude_skip_message(det.rel)
                        all_warnings.append(warning)
                        self._log("warning", warning)
        elif distributed_compiler is None and files_written > 0 and not config.dry_run:
            # Single-file strategy bypasses the distributed display formatter
            # (which has no analysis to render). Emit a minimal progress line
            # so users get a confirmation that single-file mode took effect.
            noun = "file" if files_written == 1 else "files"
            self._log(
                "progress",
                f"CLAUDE.md compiled ({files_written} {noun})",
                symbol="success",
            )

        # Display CLAUDE.md compilation output using standard formatter
        # Get proper compilation results from distributed compiler (has optimization decisions)
        # Skip formatter output when deduplication filtered out all placements to
        # avoid contradicting the "not generated" log message above.
        from ..output.formatters import CompilationFormatter
        from ..output.models import CompilationResults

        compilation_results = (
            distributed_compiler.get_compilation_results_for_display(is_dry_run=config.dry_run)
            if distributed_compiler is not None
            else None
        )
        if compilation_results and not (skip_instructions and files_written == 0):
            # Update target name for CLAUDE.md output
            formatter_results = CompilationResults(
                project_analysis=compilation_results.project_analysis,
                optimization_decisions=compilation_results.optimization_decisions,
                placement_summaries=compilation_results.placement_summaries,
                optimization_stats=compilation_results.optimization_stats,
                warnings=all_warnings,
                errors=all_errors,
                is_dry_run=config.dry_run,
                target_name="CLAUDE.md",
            )

            # Use the same formatter as AGENTS.md
            formatter = CompilationFormatter(use_color=True)
            if config.debug or config.trace:
                output = formatter.format_verbose(formatter_results)
            elif config.dry_run:
                output = formatter.format_dry_run(formatter_results)
            else:
                output = formatter.format_default(formatter_results)
            self._log("progress", output)

        # Generate summary content for result object
        summary_lines = [
            f"# CLAUDE.md Compilation Summary",  # noqa: F541
            f"",  # noqa: F541
            f"Generated {files_written} CLAUDE.md files:",
        ]
        for placement in claude_result.placements:
            rel_path = portable_relpath(placement.claude_path, self.base_dir)
            summary_lines.append(f"- {rel_path} ({len(placement.instructions)} instructions)")

        return CompilationResult(
            success=len(all_errors) == 0,
            output_path=f"CLAUDE.md: {files_written} files",
            content="\n".join(summary_lines),
            warnings=all_warnings,
            errors=all_errors,
            stats=stats,
            has_critical_security=critical_security_found,
        )

    # ------------------------------------------------------------------ #
    # GEMINI.md compilation                                                #
    # ------------------------------------------------------------------ #

    def _compile_gemini_md(
        self,
        config: CompilationConfig,
        primitives: PrimitiveCollection,
    ) -> CompilationResult:
        """Compile GEMINI.md stub that imports AGENTS.md."""
        from apm_cli.compilation.agents_compiler import CompilationResult

        from .gemini_formatter import GeminiFormatter

        gemini_formatter = GeminiFormatter(str(self.base_dir))
        gemini_result = gemini_formatter.format_distributed(primitives)

        all_warnings = self.warnings + gemini_result.warnings
        all_errors = self.errors + gemini_result.errors

        if config.dry_run:
            return CompilationResult(
                success=len(all_errors) == 0,
                output_path="Preview mode - GEMINI.md",
                content="GEMINI.md Preview: Would generate stub importing AGENTS.md",
                warnings=all_warnings,
                errors=all_errors,
                stats=gemini_result.stats,
            )

        files_written = 0
        from .output_writer import CompiledOutputWriter

        writer = CompiledOutputWriter()
        for gemini_path, content in gemini_result.content_map.items():
            try:
                writer.write(gemini_path, content)
                files_written += 1
            except OSError as e:
                all_errors.append(f"Failed to write {gemini_path}: {e!s}")

        stats = gemini_result.stats.copy()
        stats["gemini_files_written"] = files_written

        self._log("progress", "Generated GEMINI.md (imports AGENTS.md)")

        return CompilationResult(
            success=len(all_errors) == 0,
            output_path=f"GEMINI.md: {files_written} files",
            content=f"Generated {files_written} GEMINI.md stub importing AGENTS.md",
            warnings=all_warnings,
            errors=all_errors,
            stats=stats,
        )

    # ------------------------------------------------------------------ #
    # Copilot root-instructions emit / cleanup                            #
    # ------------------------------------------------------------------ #

    def _maybe_emit_copilot_root_instructions(
        self,
        config: CompilationConfig,
        primitives: PrimitiveCollection,
        result: CompilationResult,
    ) -> CompilationResult:
        """Generate .github/copilot-instructions.md for Copilot-capable targets."""
        from ..core.target_detection import should_compile_copilot_instructions_md
        from .agents_compiler import _COPILOT_ROOT_GENERATED_MARKER, _VSCODE_TARGET_ALIASES

        routing_target = "vscode" if config.target in _VSCODE_TARGET_ALIASES else config.target
        output_path = self.base_dir / ".github" / "copilot-instructions.md"
        if not should_compile_copilot_instructions_md(routing_target):
            if not config.dry_run:
                self._cleanup_copilot_root_instructions(output_path, result)
            result.stats.setdefault("copilot_root_instructions_generated", 0)
            result.stats.setdefault("copilot_root_instructions_written", 0)
            result.stats.setdefault("copilot_root_instructions_unchanged", 0)
            result.stats.setdefault("copilot_root_instructions_skipped", 0)
            result.stats.setdefault("copilot_root_instructions_removed", 0)
            return result

        global_instructions = sorted(
            [instruction for instruction in primitives.instructions if not instruction.apply_to],
            key=lambda instruction: portable_relpath(instruction.file_path, self.base_dir),
        )
        if not global_instructions:
            if not config.dry_run:
                self._cleanup_copilot_root_instructions(output_path, result)
            result.stats.setdefault("copilot_root_instructions_generated", 0)
            result.stats.setdefault("copilot_root_instructions_written", 0)
            result.stats.setdefault("copilot_root_instructions_unchanged", 0)
            result.stats.setdefault("copilot_root_instructions_skipped", 0)
            result.stats.setdefault("copilot_root_instructions_removed", 0)
            return result

        content = self._generate_copilot_root_instructions_content(global_instructions, config)

        result.stats["copilot_root_instructions_generated"] = 1
        result.stats.setdefault("copilot_root_instructions_skipped", 0)
        result.stats.setdefault("copilot_root_instructions_removed", 0)
        result.stats.setdefault("copilot_root_instructions_written", 0)
        result.stats.setdefault("copilot_root_instructions_unchanged", 0)

        # Inspect any existing file BEFORE the dry-run early-exit so that
        # `--dry-run` faithfully reports what a real run would do (skip vs
        # write vs unchanged). Reading the file here is safe in dry-run mode
        # because we never mutate it.
        try:
            existing = output_path.read_text(encoding="utf-8") if output_path.exists() else None
        except OSError as exc:
            message = f"Failed to read {output_path}: {exc}"
            self.errors.append(message)
            result.errors.append(message)
            result.success = False
            return result

        if existing is not None and _COPILOT_ROOT_GENERATED_MARKER not in existing:
            rel_path = portable_relpath(output_path, self.base_dir)
            result.warnings.append(
                f"Skipped {rel_path}: hand-authored file will not be overwritten. "
                "To regenerate, either delete or rename it, or prepend the line "
                f"'{_COPILOT_ROOT_GENERATED_MARKER}' to the top of the file. "
                "Then re-run 'apm compile'."
            )
            # The file was never compared to new content; record as
            # 'skipped', not 'unchanged'. Also reset 'generated' since no
            # output was actually emitted (or would be, on a real run).
            result.stats["copilot_root_instructions_generated"] = 0
            result.stats["copilot_root_instructions_written"] = 0
            result.stats["copilot_root_instructions_skipped"] = 1
            result.stats["copilot_root_instructions_unchanged"] = 0
            return result

        if existing == content:
            result.stats["copilot_root_instructions_written"] = 0
            result.stats["copilot_root_instructions_unchanged"] = 1
            return result

        if config.dry_run:
            return result

        from ..security.gate import WARN_POLICY, SecurityGate

        verdict = SecurityGate.scan_text(content, str(output_path), policy=WARN_POLICY)
        actionable = verdict.critical_count + verdict.warning_count
        if actionable:
            if verdict.has_critical:
                result.has_critical_security = True
            result.warnings.append(
                f"copilot-instructions.md contains {actionable} hidden character(s) "
                f"-- run 'apm audit --file {output_path}' to inspect"
            )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            result.stats["copilot_root_instructions_written"] = 1
            result.stats["copilot_root_instructions_unchanged"] = 0
            return result
        except OSError as exc:
            message = f"Failed to write {output_path}: {exc}"
            self.errors.append(message)
            result.errors.append(message)
            result.success = False
            result.stats["copilot_root_instructions_written"] = 0
            result.stats.setdefault("copilot_root_instructions_unchanged", 0)
            return result

    def _generate_copilot_root_instructions_content(
        self,
        instructions,
        config: CompilationConfig,
    ) -> str:
        """Generate root Copilot instructions content from global instruction primitives."""
        from .agents_compiler import _COPILOT_ROOT_GENERATED_MARKER

        # Functional marker and Build ID are always present (injection/drift/cleanup coupling).
        sections = [
            _COPILOT_ROOT_GENERATED_MARKER,
            BUILD_ID_PLACEHOLDER,
        ]
        if config.source_attribution:
            sections.append(f"<!-- APM Version: {get_version()} -->")
        sections.append("")

        for instruction in instructions:
            # instruction.file_path is a source-tree file; relativise it
            # against source_dir so `apm compile --root` never leaks
            # `../../` or absolute deploy-relative paths into the
            # `<!-- Source: -->` provenance comments (sources stay in $PWD
            # while writes redirect to base_dir).
            rel_path = portable_relpath(instruction.file_path, self.source_dir)
            if config.source_attribution:
                sections.append(f"<!-- Source: {rel_path} -->")
            sections.append(instruction.content.strip())
            if config.source_attribution:
                sections.append(f"<!-- End source: {rel_path} -->")
            sections.append("")

        if config.source_attribution:
            sections.append("---")
            sections.append("*This file was generated by APM CLI. Do not edit manually.*")
            sections.append("*To regenerate: `apm compile`*")
            sections.append("")

        content = "\n".join(sections)
        if config.resolve_links:
            # Rule B: resolve_markdown_links is patched at agents_compiler in tests
            from apm_cli.compilation import agents_compiler as _ac

            content = _ac.resolve_markdown_links(content, self.base_dir)
        return self._finalize_build_id(content)

    def _finalize_build_id(self, content: str) -> str:
        """Replace the build-id placeholder with a deterministic content hash."""
        lines = content.splitlines()
        try:
            idx = lines.index(BUILD_ID_PLACEHOLDER)
        except ValueError:
            return content

        hash_input_lines = [line for i, line in enumerate(lines) if i != idx]
        build_id = hashlib.sha256("\n".join(hash_input_lines).encode("utf-8")).hexdigest()[:12]
        lines[idx] = f"<!-- Build ID: {build_id} -->"
        return "\n".join(lines) + ("\n" if content.endswith("\n") else "")

    def _cleanup_copilot_root_instructions(
        self,
        output_path,
        result: CompilationResult,
    ) -> CompilationResult:
        """Remove stale generated Copilot root instructions when no longer applicable."""
        from .agents_compiler import _COPILOT_ROOT_GENERATED_MARKER

        if not output_path.exists():
            result.stats.setdefault("copilot_root_instructions_removed", 0)
            return result

        try:
            existing = output_path.read_text(encoding="utf-8")
            if _COPILOT_ROOT_GENERATED_MARKER not in existing:
                result.stats.setdefault("copilot_root_instructions_removed", 0)
                return result

            output_path.unlink()
            result.stats["copilot_root_instructions_removed"] = 1
            return result
        except OSError as exc:
            message = f"Failed to remove stale {output_path}: {exc}"
            self.errors.append(message)
            result.errors.append(message)
            result.success = False
            result.stats.setdefault("copilot_root_instructions_removed", 0)
            return result
