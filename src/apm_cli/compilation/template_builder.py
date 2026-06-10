"""Template building system for AGENTS.md compilation."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..primitives.models import Chatmode, Instruction
from ..utils.paths import portable_relpath

GLOBAL_INSTRUCTIONS_HEADING = "## Global Instructions"


@dataclass
class TemplateData:
    """Data structure for template generation."""

    instructions_content: str
    # Removed volatile timestamp for deterministic builds
    version: str
    chatmode_content: str | None = None


def render_instructions_block(
    instructions: list[Instruction],
    *,
    base_dir: Path,
    emit_instruction: Callable[[Instruction], list[str]],
    global_heading: str = GLOBAL_INSTRUCTIONS_HEADING,
) -> list[str]:
    """Render the body lines of an instructions section.

    Renders global instructions (those with no ``applyTo`` pattern) under a
    single ``global_heading`` block, then renders pattern-scoped instructions
    grouped under ``## Files matching `<pattern>``` headings (sorted by
    pattern). Within each group, instructions are sorted by file path
    relative to ``base_dir`` for deterministic output.

    The caller controls per-instruction emission via ``emit_instruction`` so
    each renderer keeps its own source-attribution format unchanged.

    Args:
        instructions: Mixed global and scoped instructions (any order).
        base_dir: Directory used as the anchor for stable sort keys.
        emit_instruction: Callback that returns the lines to emit for one
            instruction. Typically the source-attribution comment, the
            instruction body, and a trailing blank line. Empty-content
            instructions are filtered out before this is invoked.
        global_heading: Heading line for the global section.

    Returns:
        Lines that the caller will join. Empty list when ``instructions`` is
        empty.
    """
    if not instructions:
        return []

    sections: list[str] = []

    def _sort_key(inst: Instruction) -> str:
        return portable_relpath(inst.file_path, base_dir)

    globals_: list[Instruction] = []
    pattern_groups: dict[str, list[Instruction]] = {}
    for instruction in instructions:
        if not instruction.apply_to:
            globals_.append(instruction)
        else:
            pattern_groups.setdefault(instruction.apply_to, []).append(instruction)

    if globals_:
        sections.append(global_heading)
        sections.append("")
        for instruction in sorted(globals_, key=_sort_key):
            if instruction.content.strip():
                sections.extend(emit_instruction(instruction))

    for pattern, pattern_instructions in sorted(pattern_groups.items()):
        sections.append(f"## Files matching `{pattern}`")
        sections.append("")
        for instruction in sorted(pattern_instructions, key=_sort_key):
            if instruction.content.strip():
                sections.extend(emit_instruction(instruction))

    return sections


def build_attributed_instructions(
    instructions: list[Instruction],
    source_attribution: dict | None,
    base_dir: Path,
) -> list[str]:
    """Render an instructions block with optional source-attribution comments.

    Convenience wrapper around :func:`render_instructions_block` that bundles
    the common attribution-header ``_emit`` closure used by both
    :class:`~apm_cli.compilation.claude_formatter.ClaudeFormatter` and
    :class:`~apm_cli.compilation.distributed_compiler.DistributedCompiler`.

    Args:
        instructions: Instructions to render.
        source_attribution: Optional ``{str(file_path): source_label}`` map.
            When provided, each instruction is prefixed with a
            ``<!-- Source: <label> <rel_path> -->`` comment.
        base_dir: Directory used as anchor for stable sort keys.

    Returns:
        Lines ready to be joined or extended into a parent ``sections`` list.
    """

    def _emit(instruction: Instruction) -> list[str]:
        lines: list[str] = []
        if source_attribution:
            source = source_attribution.get(str(instruction.file_path), "local")
            rel_path = portable_relpath(instruction.file_path, base_dir)
            lines.append(f"<!-- Source: {source} {rel_path} -->")
        lines.append(instruction.content.strip())
        lines.append("")
        return lines

    return render_instructions_block(
        instructions,
        base_dir=base_dir,
        emit_instruction=_emit,
    )


def build_conditional_sections(
    instructions: list[Instruction],
    source_dir: Path | None = None,
) -> str:
    """Build sections grouped by applyTo patterns.

    Args:
        instructions: List of instruction primitives.
        source_dir: Root used to compute display-relative paths in
            ``<!-- Source: ... -->`` comments.  Defaults to ``Path.cwd()``;
            callers using ``apm compile --root`` should pass the source
            root so attribution paths render relative to the user's
            working directory rather than the deploy target.

    Returns:
        str: Formatted conditional sections content.
    """
    if not instructions:
        return ""

    # ``source_dir`` is the project source root.  Defaults to ``Path.cwd()``;
    # callers using ``apm compile --root`` pass the captured ``$PWD`` so
    # ``<!-- Source: ... -->`` paths render against the user's working
    # directory rather than the deploy target.
    relpath_root = source_dir if source_dir is not None else Path.cwd()

    def emit(instruction: Instruction) -> list[str]:
        try:
            if instruction.file_path.is_absolute():
                relative_path = portable_relpath(instruction.file_path, relpath_root)
            else:
                relative_path = str(instruction.file_path)
        except (ValueError, OSError):
            relative_path = instruction.file_path.as_posix()

        return [
            f"<!-- Source: {relative_path} -->",
            instruction.content.strip(),
            f"<!-- End source: {relative_path} -->",
            "",
        ]

    sections = render_instructions_block(instructions, base_dir=relpath_root, emit_instruction=emit)
    return "\n".join(sections)


def find_chatmode_by_name(chatmodes: list[Chatmode], chatmode_name: str) -> Chatmode | None:
    """Find a chatmode by name.

    Args:
        chatmodes (List[Chatmode]): List of available chatmodes.
        chatmode_name (str): Name of the chatmode to find.

    Returns:
        Optional[Chatmode]: The found chatmode, or None if not found.
    """
    for chatmode in chatmodes:
        if chatmode.name == chatmode_name:
            return chatmode
    return None


def generate_agents_md_template(template_data: TemplateData) -> str:
    """Generate the complete AGENTS.md file content.

    Args:
        template_data (TemplateData): Data for template generation.

    Returns:
        str: Complete AGENTS.md file content.
    """
    sections = []

    # Header
    sections.append("# AGENTS.md")
    sections.append("<!-- Generated by APM CLI from .apm/ primitives -->")
    from .constants import BUILD_ID_PLACEHOLDER

    sections.append(BUILD_ID_PLACEHOLDER)
    sections.append(f"<!-- APM Version: {template_data.version} -->")
    sections.append("")

    # Chatmode content (if provided)
    if template_data.chatmode_content:
        sections.append(template_data.chatmode_content.strip())
        sections.append("")

    # Instructions content (grouped by patterns)
    if template_data.instructions_content:
        sections.append(template_data.instructions_content)

    # Footer
    sections.append("---")
    sections.append("*This file was generated by APM CLI. Do not edit manually.*")
    sections.append("*To regenerate: `apm compile`*")
    sections.append("")

    return "\n".join(sections)
