"""Acceptance tests for token-overhead reduction: debug comments opt-in (#1341).

These tests verify:
1. source_attribution defaults to False (cuts per-instruction Source: comments).
2. Cosmetic debug comments (APM Version, footer) are absent by default.
   CLAUDE_HEADER is a FUNCTIONAL marker (always present, like
   _COPILOT_ROOT_GENERATED_MARKER) -- it enables stale-file removal (#1729).
3. Debug comments appear when source_attribution=True is opted in.
4. The FUNCTIONAL marker _COPILOT_ROOT_GENERATED_MARKER is ALWAYS present in
   copilot-instructions.md (drift/injection/uninstall coupling guard).
5. Build ID is always present (used for drift normalization).
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.compilation.agents_compiler import _COPILOT_ROOT_GENERATED_MARKER, CompilationConfig
from apm_cli.compilation.claude_formatter import CLAUDE_HEADER, ClaudeFormatter
from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER
from apm_cli.primitives.models import Instruction, PrimitiveCollection
from apm_cli.version import get_version

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction(
    tmp_path: Path, name: str = "test", content: str = "Test content."
) -> Instruction:
    p = tmp_path / f"{name}.instructions.md"
    p.write_text(content)
    return Instruction(
        name=name,
        file_path=p,
        description=f"{name} instruction",
        apply_to="**/*.py",
        content=content,
        author="test",
        source="local",
    )


def _make_primitives(tmp_path: Path) -> PrimitiveCollection:
    pc = PrimitiveCollection()
    pc.add_primitive(_make_instruction(tmp_path))
    return pc


# ---------------------------------------------------------------------------
# 1. Default source_attribution=False
# ---------------------------------------------------------------------------


class TestSourceAttributionDefault:
    def test_source_attribution_default_is_false(self):
        """CompilationConfig must default source_attribution to False."""
        config = CompilationConfig()
        assert config.source_attribution is False

    def test_source_attribution_from_apm_yml_default_is_false(self):
        """CompilationConfig.from_apm_yml() without overrides also defaults to False."""
        config = CompilationConfig.from_apm_yml()
        assert config.source_attribution is False


# ---------------------------------------------------------------------------
# 2. CLAUDE.md - cosmetic comments absent by default
# ---------------------------------------------------------------------------


class TestClaudeFormatterDefaultNoCosmetic:
    @pytest.fixture
    def tmp_project(self):
        d = tempfile.mkdtemp()
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def primitives(self, tmp_project):
        return _make_primitives(tmp_project)

    def test_claude_header_always_present(self, tmp_project, primitives):
        """CLAUDE_HEADER must ALWAYS appear regardless of source_attribution.

        CLAUDE_HEADER is a functional marker (not a cosmetic comment): it lets
        ``apm compile --clean`` distinguish APM-generated files from hand-authored
        ones and remove stale CLAUDE.md files (issue #1729). It must be present
        even when source_attribution=False, matching the always-present behaviour
        of _COPILOT_ROOT_GENERATED_MARKER in copilot-instructions.md.
        """
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": False}
        )
        assert result.success
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert CLAUDE_HEADER in content

    def test_no_apm_version_by_default(self, tmp_project, primitives):
        """APM Version comment must NOT appear when source_attribution=False."""
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": False}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert f"<!-- APM Version: {get_version()} -->" not in content

    def test_no_footer_by_default(self, tmp_project, primitives):
        """Footer must NOT appear when source_attribution=False."""
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": False}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert "*This file was generated by APM CLI. Do not edit manually.*" not in content
        assert "*To regenerate: `apm compile`*" not in content

    def test_build_id_always_present(self, tmp_project, primitives):
        """Build ID placeholder must ALWAYS appear (required for drift normalization).

        In unit tests the placeholder is present before write-time stabilization.
        """
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": False}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        # stabilize_build_id runs at write time, so placeholder survives here
        assert BUILD_ID_PLACEHOLDER in content


# ---------------------------------------------------------------------------
# 3. CLAUDE.md - cosmetic comments present when source_attribution=True
# ---------------------------------------------------------------------------


class TestClaudeFormatterWithAttribution:
    @pytest.fixture
    def tmp_project(self):
        d = tempfile.mkdtemp()
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def primitives(self, tmp_project):
        return _make_primitives(tmp_project)

    def test_claude_header_present_with_attribution(self, tmp_project, primitives):
        """CLAUDE_HEADER appears when source_attribution=True."""
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": True}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert CLAUDE_HEADER in content

    def test_apm_version_present_with_attribution(self, tmp_project, primitives):
        """APM Version comment appears when source_attribution=True."""
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": True}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert f"<!-- APM Version: {get_version()} -->" in content

    def test_footer_present_with_attribution(self, tmp_project, primitives):
        """Footer appears when source_attribution=True."""
        formatter = ClaudeFormatter(str(tmp_project))
        placement_map = {tmp_project: list(primitives.instructions)}
        result = formatter.format_distributed(
            primitives, placement_map, {"source_attribution": True}
        )
        content = result.content_map[tmp_project / "CLAUDE.md"]
        assert "*This file was generated by APM CLI. Do not edit manually.*" in content
        assert "*To regenerate: `apm compile`*" in content


# ---------------------------------------------------------------------------
# 4. Coupling guard - _COPILOT_ROOT_GENERATED_MARKER always in copilot-instructions.md
# ---------------------------------------------------------------------------


class TestCopilotRootMarkerAlwaysPresent:
    """Regression guard: the functional marker must survive regardless of source_attribution."""

    @pytest.fixture
    def tmp_project(self):
        d = tempfile.mkdtemp()
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    def _compile_root_instructions(self, tmp_project: Path, source_attribution: bool) -> str:
        """Call _generate_copilot_root_instructions_content and return content."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler

        compiler = AgentsCompiler(str(tmp_project))
        config = CompilationConfig(source_attribution=source_attribution)
        instructions = [_make_instruction(tmp_project)]
        return compiler._generate_copilot_root_instructions_content(instructions, config)

    def test_functional_marker_present_when_attribution_false(self, tmp_project):
        """_COPILOT_ROOT_GENERATED_MARKER must be in output even when attribution=False."""
        content = self._compile_root_instructions(tmp_project, source_attribution=False)
        assert _COPILOT_ROOT_GENERATED_MARKER in content

    def test_functional_marker_present_when_attribution_true(self, tmp_project):
        """_COPILOT_ROOT_GENERATED_MARKER must be in output when attribution=True."""
        content = self._compile_root_instructions(tmp_project, source_attribution=True)
        assert _COPILOT_ROOT_GENERATED_MARKER in content

    def test_build_id_present_regardless_of_attribution(self, tmp_project):
        """Build ID must appear (drift normalization uses it) regardless of attribution flag."""
        for flag in (True, False):
            content = self._compile_root_instructions(tmp_project, source_attribution=flag)
            assert "<!-- Build ID:" in content

    def test_apm_version_absent_when_attribution_false(self, tmp_project):
        """APM Version cosmetic line must be absent when attribution=False."""
        content = self._compile_root_instructions(tmp_project, source_attribution=False)
        assert f"<!-- APM Version: {get_version()} -->" not in content

    def test_apm_version_present_when_attribution_true(self, tmp_project):
        """APM Version line must be present when attribution=True."""
        content = self._compile_root_instructions(tmp_project, source_attribution=True)
        assert f"<!-- APM Version: {get_version()} -->" in content

    def test_footer_absent_when_attribution_false(self, tmp_project):
        """Footer must be absent when attribution=False."""
        content = self._compile_root_instructions(tmp_project, source_attribution=False)
        assert "*This file was generated by APM CLI. Do not edit manually.*" not in content

    def test_footer_present_when_attribution_true(self, tmp_project):
        """Footer must be present when attribution=True."""
        content = self._compile_root_instructions(tmp_project, source_attribution=True)
        assert "*This file was generated by APM CLI. Do not edit manually.*" in content

    def test_source_comments_absent_when_attribution_false(self, tmp_project):
        """Per-instruction Source: comments absent when attribution=False."""
        content = self._compile_root_instructions(tmp_project, source_attribution=False)
        assert "<!-- Source:" not in content

    def test_source_comments_present_when_attribution_true(self, tmp_project):
        """Per-instruction Source: comments present when attribution=True."""
        content = self._compile_root_instructions(tmp_project, source_attribution=True)
        assert "<!-- Source:" in content
