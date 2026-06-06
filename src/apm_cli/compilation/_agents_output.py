"""Mixin: output-writing and display methods for AgentsCompiler.

Extracted from agents_compiler.AgentsCompiler to stay under the 800-line
guardrail (Strangler Stage 2 / issue #1078).

Rule B routing
--------------
``_logger`` is patched at ``apm_cli.compilation.agents_compiler._logger`` in
tests.  Any moved method that calls ``_logger`` does so via a function-level
late import:

    from apm_cli.compilation import agents_compiler as _ac
    _ac._logger.debug(...)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agents_compiler import CompilationConfig

from ..primitives.models import PrimitiveCollection
from ..utils.paths import portable_relpath
from .constants import BUILD_ID_PLACEHOLDER  # noqa: F401 (re-export convenience)
from .template_builder import TemplateData


class _AgentsOutputMixin:
    """Mixin: file-write, stats, display, and summary methods."""

    def _write_output_file(self, output_path: str, content: str) -> None:
        """Write the generated content to the output file (full-file mode).

        Args:
            output_path (str): Path to write the output.
            content (str): Content to write.
        """
        from .output_writer import CompiledOutputWriter

        try:
            CompiledOutputWriter().write(Path(output_path), content)
        except OSError as e:
            self.errors.append(f"Failed to write output file {output_path}: {e!s}")

    def _write_output_file_with_config(
        self,
        output_path: str,
        content: str,
        config: CompilationConfig,
    ) -> None:
        """Write generated content, honouring agents_md_mode from config.

        In ``full`` mode (default) the entire file is replaced.
        In ``managed_section`` mode only the text between the configured
        start/end markers is replaced; everything else is preserved.

        Args:
            output_path (str): Path to write the output.
            content (str): Generated content for this compilation.
            config (CompilationConfig): Compilation configuration.
        """
        from .managed_section import ManagedSectionError, apply_managed_section
        from .output_writer import CompiledOutputWriter

        if config.agents_md_mode == "managed_section":
            target = Path(output_path)
            if not target.is_file():
                raise ManagedSectionError(
                    f"{target} does not exist yet. "
                    "Create it with the managed-section markers first, "
                    "or set agents_md.mode: full in apm.yml for initial generation."
                )
            existing = target.read_text(encoding="utf-8")
            try:
                content = apply_managed_section(
                    existing,
                    content,
                    config.agents_md_start_marker,
                    config.agents_md_end_marker,
                )
            except ManagedSectionError as exc:
                raise ManagedSectionError(f"[{target}] {exc}") from exc
        elif config.agents_md_mode != "full":
            raise ValueError(
                f"Unknown agents_md.mode {config.agents_md_mode!r}. "
                "Supported values: 'full', 'managed_section'."
            )

        try:
            CompiledOutputWriter().write(Path(output_path), content)
        except OSError as e:
            self.errors.append(f"Failed to write output file {output_path}: {e!s}")

    def _compile_stats(
        self, primitives: PrimitiveCollection, template_data: TemplateData
    ) -> dict[str, Any]:
        """Compile statistics about the compilation.

        Args:
            primitives (PrimitiveCollection): Discovered primitives.
            template_data (TemplateData): Generated template data.

        Returns:
            Dict[str, Any]: Compilation statistics.
        """
        return {
            "primitives_found": primitives.count(),
            "chatmodes": len(primitives.chatmodes),
            "instructions": len(primitives.instructions),
            "contexts": len(primitives.contexts),
            "content_length": len(template_data.instructions_content),
            # timestamp removed
            "version": template_data.version,
        }

    def _write_distributed_file(
        self,
        agents_path: Path,
        content: str,
        config: CompilationConfig,
    ) -> None:
        """Write a distributed AGENTS.md file with constitution injection support.

        Args:
            agents_path (Path): Path to write the AGENTS.md file.
            content (str): Content to write.
            config (CompilationConfig): Compilation configuration.
        """
        try:
            # Handle constitution injection for distributed files
            final_content = content

            if config.with_constitution:
                # Try to inject constitution if available
                try:
                    from .injector import ConstitutionInjector

                    injector = ConstitutionInjector(str(agents_path.parent))
                    final_content, c_status, c_hash = injector.inject(  # noqa: RUF059
                        content, with_constitution=True, output_path=agents_path
                    )
                except Exception as exc:
                    # Rule B: _logger is patched at agents_compiler._logger in tests
                    from apm_cli.compilation import agents_compiler as _ac

                    _ac._logger.debug("Constitution injection failed for %s: %s", agents_path, exc)

            from .output_writer import CompiledOutputWriter

            CompiledOutputWriter().write(agents_path, final_content)

        except OSError as e:
            raise OSError(f"Failed to write distributed AGENTS.md file {agents_path}: {e!s}")  # noqa: B904

    def _display_placement_preview(self, distributed_result) -> None:
        """Display placement preview for --show-placement mode.

        Args:
            distributed_result: Result from distributed compilation.
        """
        self._log("progress", "Distributed AGENTS.md Placement Preview:")
        self._log("progress", "")

        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            self._log("verbose_detail", f"{rel_path}")
            self._log("verbose_detail", f"   Instructions: {len(placement.instructions)}")
            self._log(
                "verbose_detail", f"   Patterns: {', '.join(sorted(placement.coverage_patterns))}"
            )
            if placement.source_attribution:
                sources = set(placement.source_attribution.values())
                self._log("verbose_detail", f"   Sources: {', '.join(sorted(sources))}")
            self._log("verbose_detail", "")

    def _display_trace_info(self, distributed_result, primitives: PrimitiveCollection) -> None:
        """Display detailed trace information for --trace mode.

        Args:
            distributed_result: Result from distributed compilation.
            primitives (PrimitiveCollection): Full primitive collection.
        """
        self._log("progress", "Distributed Compilation Trace:")
        self._log("progress", "")

        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            self._log("verbose_detail", f"{rel_path}")

            for instruction in placement.instructions:
                source = getattr(instruction, "source", "local")
                # instruction.file_path is a source-tree file; relativise
                # against source_dir so `apm compile --root` produces
                # human-readable paths in verbose output.
                inst_path = portable_relpath(instruction.file_path, self.source_dir)

                self._log(
                    "verbose_detail",
                    f"   * {instruction.apply_to or 'no pattern'} <- {source} {inst_path}",
                )
            self._log("verbose_detail", "")

    def _generate_placement_summary(self, distributed_result) -> str:
        """Generate a text summary of placement results.

        Args:
            distributed_result: Result from distributed compilation.

        Returns:
            str: Text summary of placements.
        """
        lines = ["Distributed AGENTS.md Placement Summary:", ""]

        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            lines.append(f"{rel_path}")
            lines.append(f"   Instructions: {len(placement.instructions)}")
            lines.append(f"   Patterns: {', '.join(sorted(placement.coverage_patterns))}")
            lines.append("")

        lines.append(f"Total AGENTS.md files: {len(distributed_result.placements)}")
        return "\n".join(lines)

    def _generate_distributed_summary(
        self,
        distributed_result,
        config: CompilationConfig,
    ) -> str:
        """Generate a summary of distributed compilation results.

        Args:
            distributed_result: Result from distributed compilation.
            config (CompilationConfig): Compilation configuration.

        Returns:
            str: Summary content.
        """
        lines = [
            "# Distributed AGENTS.md Compilation Summary",
            "",
            f"Generated {len(distributed_result.placements)} AGENTS.md files:",
            "",
        ]

        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            lines.append(f"- {rel_path} ({len(placement.instructions)} instructions)")

        lines.extend(
            [
                "",
                f"Total instructions: {distributed_result.stats.get('total_instructions_placed', 0)}",
                f"Total patterns: {distributed_result.stats.get('total_patterns_covered', 0)}",
                "",
                "Use 'apm compile --single-agents' for traditional single-file compilation.",
            ]
        )

        return "\n".join(lines)
