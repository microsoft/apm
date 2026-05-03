"""Regression tests for issue #1072 -- global instructions silently dropped.

Issue: ``apm compile`` reports global instructions (no ``applyTo``) as placed
at ``./AGENTS.md`` with ``rel: 100%``, but the rendered file did not contain
their content. The optimizer was correct; the renderers filtered globals out.

These tests pin the corrected behavior across all three renderers
(``build_conditional_sections``, ``DistributedAgentsCompiler``,
``ClaudeFormatter``) plus the shared ``render_instructions_block`` helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.compilation.claude_formatter import ClaudeFormatter
from apm_cli.compilation.distributed_compiler import DistributedAgentsCompiler
from apm_cli.compilation.template_builder import (
    GLOBAL_INSTRUCTIONS_HEADING,
    build_conditional_sections,
    render_instructions_block,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection


def _make_instruction(
    *,
    name: str,
    file_path: Path,
    apply_to: str,
    content: str,
    description: str = "test",
    source: str = "local",
) -> Instruction:
    inst = Instruction(
        name=name,
        file_path=file_path,
        description=description,
        apply_to=apply_to,
        content=content,
        author="test",
        source=source,
    )
    return inst


class TestRenderInstructionsBlock:
    """Unit tests for the shared ``render_instructions_block`` helper."""

    @staticmethod
    def _passthrough_emit(instruction: Instruction) -> list[str]:
        return [instruction.content.strip(), ""]

    def test_empty_instructions_returns_empty_list(self, tmp_path):
        assert (
            render_instructions_block(
                [], base_dir=tmp_path, emit_instruction=self._passthrough_emit
            )
            == []
        )

    def test_only_scoped_omits_global_heading(self, tmp_path):
        instructions = [
            _make_instruction(
                name="py", file_path=tmp_path / "py.md", apply_to="**/*.py", content="A"
            ),
        ]

        lines = render_instructions_block(
            instructions, base_dir=tmp_path, emit_instruction=self._passthrough_emit
        )

        assert GLOBAL_INSTRUCTIONS_HEADING not in lines
        assert "## Files matching `**/*.py`" in lines

    def test_only_globals_emits_global_heading_no_pattern_heading(self, tmp_path):
        instructions = [
            _make_instruction(name="g", file_path=tmp_path / "g.md", apply_to="", content="GLOBAL"),
        ]

        lines = render_instructions_block(
            instructions, base_dir=tmp_path, emit_instruction=self._passthrough_emit
        )

        assert GLOBAL_INSTRUCTIONS_HEADING in lines
        assert not any(line.startswith("## Files matching") for line in lines)
        assert "GLOBAL" in lines

    def test_globals_render_before_scoped_groups(self, tmp_path):
        """Issue #1072 core scenario: global content must appear AND must
        come before pattern-scoped sections so the structural order stays
        deterministic."""
        instructions = [
            _make_instruction(
                name="scoped",
                file_path=tmp_path / "scoped.md",
                apply_to="*.md",
                content="SCOPED",
            ),
            _make_instruction(
                name="global",
                file_path=tmp_path / "global.md",
                apply_to="",
                content="GLOBAL",
            ),
        ]

        lines = render_instructions_block(
            instructions, base_dir=tmp_path, emit_instruction=self._passthrough_emit
        )

        text = "\n".join(lines)
        assert "GLOBAL" in text
        assert "SCOPED" in text
        assert text.index(GLOBAL_INSTRUCTIONS_HEADING) < text.index("## Files matching `*.md`")

    def test_globals_sorted_by_relative_path(self, tmp_path):
        instructions = [
            _make_instruction(
                name="z",
                file_path=tmp_path / "z.md",
                apply_to="",
                content="Z",
            ),
            _make_instruction(
                name="a",
                file_path=tmp_path / "a.md",
                apply_to="",
                content="A",
            ),
        ]

        lines = render_instructions_block(
            instructions, base_dir=tmp_path, emit_instruction=self._passthrough_emit
        )
        text = "\n".join(lines)

        # ``a.md`` sorts before ``z.md`` regardless of input order.
        assert text.index("A") < text.index("Z")

    def test_global_with_empty_content_skipped(self, tmp_path):
        instructions = [
            _make_instruction(
                name="empty",
                file_path=tmp_path / "empty.md",
                apply_to="",
                content="   \n\n",
            ),
            _make_instruction(
                name="filled",
                file_path=tmp_path / "filled.md",
                apply_to="",
                content="REAL",
            ),
        ]

        lines = render_instructions_block(
            instructions, base_dir=tmp_path, emit_instruction=self._passthrough_emit
        )

        assert "REAL" in lines
        # Empty content stripped -- no trace of "empty.md" via the passthrough emitter
        assert "" in lines  # blank separators are fine
        assert sum(1 for line in lines if line == "REAL") == 1

    def test_emit_callback_only_invoked_for_non_empty_content(self, tmp_path):
        seen: list[str] = []

        def tracking_emit(instruction: Instruction) -> list[str]:
            seen.append(instruction.name)
            return [instruction.content.strip(), ""]

        instructions = [
            _make_instruction(name="empty", file_path=tmp_path / "e.md", apply_to="", content="\n"),
            _make_instruction(name="real", file_path=tmp_path / "r.md", apply_to="", content="X"),
        ]

        render_instructions_block(instructions, base_dir=tmp_path, emit_instruction=tracking_emit)

        assert seen == ["real"]

    def test_custom_global_heading(self, tmp_path):
        instructions = [
            _make_instruction(name="g", file_path=tmp_path / "g.md", apply_to="", content="X"),
        ]

        lines = render_instructions_block(
            instructions,
            base_dir=tmp_path,
            emit_instruction=self._passthrough_emit,
            global_heading="## Always",
        )

        assert "## Always" in lines


class TestBuildConditionalSectionsIncludesGlobals:
    """Issue #1072 reproduction via the template-path renderer."""

    def test_global_content_appears_in_output(self, tmp_path):
        instructions = [
            _make_instruction(
                name="global",
                file_path=tmp_path / ".apm/instructions/global.instructions.md",
                apply_to="",
                content="This should appear in AGENTS.md.",
            ),
            _make_instruction(
                name="scoped",
                file_path=tmp_path / ".apm/instructions/scoped.instructions.md",
                apply_to="*.md",
                content="This appears in AGENTS.md for *.md files.",
            ),
        ]

        result = build_conditional_sections(instructions)

        assert "This should appear in AGENTS.md." in result
        assert "This appears in AGENTS.md for *.md files." in result
        assert GLOBAL_INSTRUCTIONS_HEADING in result
        assert "## Files matching `*.md`" in result

    def test_only_scoped_does_not_emit_global_heading(self, tmp_path):
        instructions = [
            _make_instruction(
                name="scoped",
                file_path=tmp_path / "s.md",
                apply_to="*.md",
                content="S",
            ),
        ]

        result = build_conditional_sections(instructions)

        assert GLOBAL_INSTRUCTIONS_HEADING not in result
        assert "## Files matching `*.md`" in result


class TestDistributedCompilerIncludesGlobals:
    """Issue #1072: ``apm compile -t agents`` must include global content."""

    def test_global_instruction_appears_in_agents_md(self, tmp_path):
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            _make_instruction(
                name="global",
                file_path=tmp_path / ".apm/instructions/global.instructions.md",
                apply_to="",
                content="GLOBAL_BODY",
            )
        )
        primitives.add_primitive(
            _make_instruction(
                name="scoped",
                file_path=tmp_path / ".apm/instructions/scoped.instructions.md",
                apply_to="*.md",
                content="SCOPED_BODY",
            )
        )

        compiler = DistributedAgentsCompiler(str(tmp_path))
        result = compiler.compile_distributed(primitives, config={"dry_run": True})

        agents_path = tmp_path / "AGENTS.md"
        assert result.success
        assert agents_path in result.content_map
        content = result.content_map[agents_path]
        assert "GLOBAL_BODY" in content
        assert "SCOPED_BODY" in content
        assert GLOBAL_INSTRUCTIONS_HEADING in content

    def test_global_appears_before_scoped_section_in_agents_md(self, tmp_path):
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            _make_instruction(
                name="scoped",
                file_path=tmp_path / "s.md",
                apply_to="*.md",
                content="SCOPED_BODY",
            )
        )
        primitives.add_primitive(
            _make_instruction(
                name="global",
                file_path=tmp_path / "g.md",
                apply_to="",
                content="GLOBAL_BODY",
            )
        )

        compiler = DistributedAgentsCompiler(str(tmp_path))
        result = compiler.compile_distributed(primitives, config={"dry_run": True})
        content = result.content_map[tmp_path / "AGENTS.md"]

        assert content.index("GLOBAL_BODY") < content.index("SCOPED_BODY")

    def test_global_carries_source_attribution_comment(self, tmp_path):
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            _make_instruction(
                name="global",
                file_path=tmp_path / "g.md",
                apply_to="",
                content="X",
                source="dependency:foo/bar",
            )
        )

        compiler = DistributedAgentsCompiler(str(tmp_path))
        result = compiler.compile_distributed(
            primitives, config={"dry_run": True, "source_attribution": True}
        )
        content = result.content_map[tmp_path / "AGENTS.md"]

        # Source attribution comment must be present for the global instruction.
        assert "<!-- Source:" in content
        assert "g.md" in content


class TestClaudeFormatterIncludesGlobals:
    """Issue #1072: ``apm compile -t claude`` must include global content."""

    def test_global_instruction_appears_in_claude_md(self, tmp_path):
        primitives = PrimitiveCollection()
        global_inst = _make_instruction(
            name="global",
            file_path=tmp_path / ".apm/instructions/global.instructions.md",
            apply_to="",
            content="GLOBAL_BODY",
        )
        scoped_inst = _make_instruction(
            name="scoped",
            file_path=tmp_path / ".apm/instructions/scoped.instructions.md",
            apply_to="*.md",
            content="SCOPED_BODY",
        )
        primitives.add_primitive(global_inst)
        primitives.add_primitive(scoped_inst)

        formatter = ClaudeFormatter(str(tmp_path))
        placement_map = {tmp_path: [global_inst, scoped_inst]}
        result = formatter.format_distributed(primitives, placement_map)

        claude_path = tmp_path / "CLAUDE.md"
        assert claude_path in result.content_map
        content = result.content_map[claude_path]
        assert "GLOBAL_BODY" in content
        assert "SCOPED_BODY" in content
        # Project Standards top-level header still present, with global heading nested.
        assert "# Project Standards" in content
        assert GLOBAL_INSTRUCTIONS_HEADING in content
        assert content.index("GLOBAL_BODY") < content.index("SCOPED_BODY")

    def test_only_globals_emits_general_section_only(self, tmp_path):
        primitives = PrimitiveCollection()
        inst = _make_instruction(name="g", file_path=tmp_path / "g.md", apply_to="", content="ONLY")
        primitives.add_primitive(inst)

        formatter = ClaudeFormatter(str(tmp_path))
        placement_map = {tmp_path: [inst]}
        result = formatter.format_distributed(primitives, placement_map)
        content = result.content_map[tmp_path / "CLAUDE.md"]

        assert "ONLY" in content
        assert GLOBAL_INSTRUCTIONS_HEADING in content
        assert "## Files matching" not in content


@pytest.mark.parametrize(
    "renderer",
    ["template", "distributed", "claude"],
)
def test_global_instruction_is_never_silently_dropped(tmp_path, renderer):
    """Cross-renderer parity guard: every renderer must surface the global
    instruction's content. This pins the architectural fix for issue #1072
    so future renderers cannot regress to the silent-drop behavior."""
    inst = _make_instruction(
        name="g",
        file_path=tmp_path / "g.md",
        apply_to="",
        content="GLOBAL_SENTINEL",
    )

    if renderer == "template":
        text = build_conditional_sections([inst])
    elif renderer == "distributed":
        primitives = PrimitiveCollection()
        primitives.add_primitive(inst)
        compiler = DistributedAgentsCompiler(str(tmp_path))
        result = compiler.compile_distributed(primitives, config={"dry_run": True})
        text = result.content_map[tmp_path / "AGENTS.md"]
    else:
        primitives = PrimitiveCollection()
        primitives.add_primitive(inst)
        formatter = ClaudeFormatter(str(tmp_path))
        result = formatter.format_distributed(primitives, {tmp_path: [inst]})
        text = result.content_map[tmp_path / "CLAUDE.md"]

    assert "GLOBAL_SENTINEL" in text
