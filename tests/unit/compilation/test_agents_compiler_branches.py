"""Unit tests for apm_cli.compilation.agents_compiler.

Focuses on:
- CompilationConfig.__post_init__ (single_agents=True triggers strategy="single-file")
- AgentsCompiler.compile() unknown frozenset target families
- AgentsCompiler.compile() unknown string target
- AgentsCompiler.compile() no-results (agent-skills, windsurf)
- AgentsCompiler.compile() top-level exception handler
- AgentsCompiler._compile_gemini_md() dry_run + non-dry_run
- AgentsCompiler._maybe_emit_copilot_root_instructions() all branches:
  - non-compilable target (no copilot instructions)
  - no global instructions
  - hand-authored file skipped
  - unchanged (existing == new content)
  - dry_run early return after content generation
  - OSError reading existing file
  - OSError writing file
  - security gate warning
- AgentsCompiler._cleanup_copilot_root_instructions()
  - file does not exist
  - file not APM-generated (skip)
  - APM-generated file removed
  - OSError on remove
- AgentsCompiler._finalize_build_id()
  - placeholder present
  - placeholder absent
- AgentsCompiler._generate_copilot_root_instructions_content()
- AgentsCompiler.generate_output() with resolve_links=False
- AgentsCompiler._generate_template_data() with chatmode not found
- AgentsCompiler._display_placement_preview and _display_trace_info
- AgentsCompiler._compile_stats
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.compilation.agents_compiler import (
    _COPILOT_ROOT_GENERATED_MARKER,
    AgentsCompiler,
    CompilationConfig,
    CompilationResult,
    compile_agents_md,
)
from apm_cli.primitives.models import Instruction, PrimitiveCollection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instruction(
    name: str = "test",
    apply_to: str | None = "**/*.py",
    content: str = "Use type hints.",
    file_path: Path | None = None,
) -> Instruction:
    if file_path is None:
        file_path = Path(f"/tmp/{name}.instructions.md")
    return Instruction(
        name=name,
        file_path=file_path,
        description="Test instruction",
        apply_to=apply_to,
        content=content,
        author="test",
        version="1.0",
    )


def _make_primitives(*instructions: Instruction) -> PrimitiveCollection:
    col = PrimitiveCollection()
    for inst in instructions:
        col.add_primitive(inst)
    return col


def _make_result(
    success: bool = True,
    output_path: str = "out",
    content: str = "c",
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    stats: dict | None = None,
) -> CompilationResult:
    return CompilationResult(
        success=success,
        output_path=output_path,
        content=content,
        warnings=warnings or [],
        errors=errors or [],
        stats=stats or {},
    )


# ---------------------------------------------------------------------------
# CompilationConfig.__post_init__
# ---------------------------------------------------------------------------


class TestCompilationConfigPostInit(unittest.TestCase):
    def test_single_agents_true_sets_strategy(self) -> None:
        config = CompilationConfig(single_agents=True)
        self.assertEqual(config.strategy, "single-file")

    def test_single_agents_false_keeps_default_strategy(self) -> None:
        config = CompilationConfig(single_agents=False)
        self.assertEqual(config.strategy, "distributed")

    def test_exclude_defaults_to_empty_list(self) -> None:
        config = CompilationConfig()
        self.assertEqual(config.exclude, [])

    def test_exclude_none_becomes_empty_list(self) -> None:
        config = CompilationConfig(exclude=None)
        self.assertEqual(config.exclude, [])


# ---------------------------------------------------------------------------
# AgentsCompiler.compile() — unknown target paths
# ---------------------------------------------------------------------------


class TestAgentsCompilerUnknownTarget(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unknown_string_target_returns_failure(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="nonexistent-target", strategy="single-file")
        primitives = _make_primitives()
        result = compiler.compile(config, primitives)
        self.assertFalse(result.success)
        self.assertTrue(any("Unknown compilation target" in e for e in result.errors))

    def test_unknown_frozenset_family_returns_failure(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target=frozenset({"bogus_family"}))
        primitives = _make_primitives()
        result = compiler.compile(config, primitives)
        self.assertFalse(result.success)
        self.assertTrue(any("Unknown compilation target family" in e for e in result.errors))

    def test_valid_frozenset_with_agents_succeeds(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target=frozenset({"agents"}), strategy="single-file")
        primitives = _make_primitives(
            _make_instruction(file_path=Path(self.tmp) / "inst.instructions.md")
        )
        with patch.object(compiler, "_compile_agents_md", return_value=_make_result()) as mock:
            result = compiler.compile(config, primitives)
        mock.assert_called_once()
        self.assertTrue(result.success)


# ---------------------------------------------------------------------------
# AgentsCompiler.compile() — no-results path
# ---------------------------------------------------------------------------


class TestAgentsCompilerNoResults(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cursor_target_routes_through_agents_compiler(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="cursor")
        primitives = _make_primitives()
        result = compiler.compile(config, primitives)
        self.assertTrue(result.success)
        self.assertTrue(result.output_path.startswith("Distributed:"))

    def test_agent_skills_target_logs_skip(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="agent-skills")
        primitives = _make_primitives()
        logger = MagicMock()
        result = compiler.compile(config, primitives, logger=logger)
        self.assertTrue(result.success)
        logger.progress.assert_called()

    def test_windsurf_target_returns_empty_success(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="windsurf")
        primitives = _make_primitives()
        result = compiler.compile(config, primitives)
        self.assertTrue(result.success)

    def test_codex_target_returns_empty_success(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="codex")
        primitives = _make_primitives()
        result = compiler.compile(config, primitives)
        self.assertTrue(result.success)


# ---------------------------------------------------------------------------
# AgentsCompiler.compile() — top-level exception handler
# ---------------------------------------------------------------------------


class TestAgentsCompilerExceptionHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exception_in_compile_agents_md_returns_failure(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", strategy="single-file")
        primitives = _make_primitives()
        with patch.object(compiler, "_compile_agents_md", side_effect=RuntimeError("boom")):
            result = compiler.compile(config, primitives)
        self.assertFalse(result.success)
        self.assertTrue(any("boom" in e for e in result.errors))


# ---------------------------------------------------------------------------
# AgentsCompiler._compile_gemini_md()
# ---------------------------------------------------------------------------


class TestCompileGeminiMd(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_gemini_result(self, n_files: int = 1) -> MagicMock:
        result = MagicMock()
        result.success = True
        result.warnings = []
        result.errors = []
        result.stats = {}
        content_map: dict = {}
        for i in range(n_files):
            p = Path(self.tmp) / f"sub{i}" / "GEMINI.md"
            content_map[p] = f"@./AGENTS.md  # file {i}"
        result.content_map = content_map
        return result

    def test_dry_run_returns_preview(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="gemini", dry_run=True)
        primitives = _make_primitives()

        mock_formatter = MagicMock()
        mock_formatter.format_distributed.return_value = self._make_gemini_result()

        with patch(
            "apm_cli.compilation.gemini_formatter.GeminiFormatter", return_value=mock_formatter
        ):
            result = compiler._compile_gemini_md(config, primitives)

        self.assertTrue(result.success)
        self.assertIn("Preview", result.output_path)
        self.assertIn("GEMINI.md", result.content)

    def test_non_dry_run_writes_files(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="gemini", dry_run=False)
        primitives = _make_primitives()

        gemini_result = self._make_gemini_result(n_files=1)

        # Create the target directory so the write can succeed
        for p in gemini_result.content_map:
            p.parent.mkdir(parents=True, exist_ok=True)

        mock_formatter = MagicMock()
        mock_formatter.format_distributed.return_value = gemini_result

        with (
            patch(
                "apm_cli.compilation.gemini_formatter.GeminiFormatter", return_value=mock_formatter
            ),
            patch("apm_cli.compilation.output_writer.CompiledOutputWriter.write") as mock_write,
        ):
            result = compiler._compile_gemini_md(config, primitives)

        mock_write.assert_called()
        self.assertTrue(result.success)

    def test_write_oserror_adds_error(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="gemini", dry_run=False)
        primitives = _make_primitives()

        gemini_result = self._make_gemini_result(n_files=1)

        mock_formatter = MagicMock()
        mock_formatter.format_distributed.return_value = gemini_result

        with (
            patch(
                "apm_cli.compilation.gemini_formatter.GeminiFormatter", return_value=mock_formatter
            ),
            patch(
                "apm_cli.compilation.output_writer.CompiledOutputWriter.write",
                side_effect=OSError("write denied"),
            ),
        ):
            result = compiler._compile_gemini_md(config, primitives)

        self.assertFalse(result.success)
        self.assertTrue(any("write denied" in e for e in result.errors))


# ---------------------------------------------------------------------------
# AgentsCompiler._finalize_build_id()
# ---------------------------------------------------------------------------


class TestFinalizeBuildId(unittest.TestCase):
    def test_with_placeholder_replaced_by_hash(self) -> None:
        from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER

        compiler = AgentsCompiler("/tmp")
        content = f"Line before\n{BUILD_ID_PLACEHOLDER}\nLine after\n"
        result = compiler._finalize_build_id(content)
        self.assertNotIn(BUILD_ID_PLACEHOLDER, result)
        self.assertIn("<!-- Build ID:", result)

    def test_without_placeholder_unchanged(self) -> None:
        compiler = AgentsCompiler("/tmp")
        content = "No placeholder here\nLine 2\n"
        result = compiler._finalize_build_id(content)
        self.assertEqual(result, content)

    def test_hash_is_deterministic(self) -> None:
        from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER

        compiler = AgentsCompiler("/tmp")
        content = f"A\n{BUILD_ID_PLACEHOLDER}\nB\n"
        r1 = compiler._finalize_build_id(content)
        r2 = compiler._finalize_build_id(content)
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------
# AgentsCompiler._generate_copilot_root_instructions_content()
# ---------------------------------------------------------------------------


class TestGenerateCopilotRootInstructionsContent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_content_includes_generated_marker(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Global rules.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        config = CompilationConfig(resolve_links=False)
        content = compiler._generate_copilot_root_instructions_content([inst], config)
        self.assertIn(_COPILOT_ROOT_GENERATED_MARKER, content)

    def test_content_includes_instruction_text(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Specific global rule.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        config = CompilationConfig(resolve_links=False)
        content = compiler._generate_copilot_root_instructions_content([inst], config)
        self.assertIn("Specific global rule.", content)

    def test_content_has_build_id_comment(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        inst = _make_instruction(
            name="global",
            apply_to=None,
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        config = CompilationConfig(resolve_links=False)
        content = compiler._generate_copilot_root_instructions_content([inst], config)
        self.assertIn("<!-- Build ID:", content)


# ---------------------------------------------------------------------------
# AgentsCompiler._maybe_emit_copilot_root_instructions()
# ---------------------------------------------------------------------------


class TestMaybeEmitCopilotRootInstructions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _github_dir(self) -> Path:
        return Path(self.tmp) / ".github"

    def _output_path(self) -> Path:
        return self._github_dir() / "copilot-instructions.md"

    def test_non_compilable_target_returns_result_unchanged(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="claude")
        base_result = _make_result(stats={})
        primitives = _make_primitives()
        result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)
        self.assertIs(result, base_result)
        self.assertIn("copilot_root_instructions_generated", result.stats)

    def test_no_global_instructions_cleanup_called(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode")
        # Only per-file instruction (has apply_to), no global (apply_to=None)
        primitives = _make_primitives(
            _make_instruction(apply_to="**/*.py", file_path=Path(self.tmp) / "per.instructions.md")
        )
        base_result = _make_result(stats={})
        result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)
        self.assertIn("copilot_root_instructions_generated", result.stats)
        self.assertEqual(result.stats["copilot_root_instructions_generated"], 0)

    def test_hand_authored_file_skipped(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", resolve_links=False)

        # Create a hand-authored file (no APM marker)
        out = self._output_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# My hand-authored instructions\n", encoding="utf-8")

        primitives = _make_primitives(
            _make_instruction(
                name="global",
                apply_to=None,
                content="Global.",
                file_path=Path(self.tmp) / "global.instructions.md",
            )
        )
        base_result = _make_result(stats={})
        result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)

        self.assertEqual(result.stats["copilot_root_instructions_skipped"], 1)
        self.assertEqual(result.stats["copilot_root_instructions_written"], 0)
        self.assertTrue(any("hand-authored" in w for w in result.warnings))

    def test_unchanged_content_not_rewritten(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", resolve_links=False, with_constitution=False)

        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Global rule.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        primitives = _make_primitives(inst)

        # First run to produce the file
        out = self._output_path()
        out.parent.mkdir(parents=True, exist_ok=True)

        # Pre-generate the exact content that will be produced
        existing_content = compiler._generate_copilot_root_instructions_content(
            sorted(
                [
                    instruction
                    for instruction in primitives.instructions
                    if not instruction.apply_to
                ],
                key=lambda i: str(i.file_path),
            ),
            config,
        )
        out.write_text(existing_content, encoding="utf-8")

        base_result = _make_result(stats={})
        result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)

        self.assertEqual(result.stats["copilot_root_instructions_unchanged"], 1)
        self.assertEqual(result.stats["copilot_root_instructions_written"], 0)

    def test_dry_run_returns_without_writing(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", dry_run=True, resolve_links=False)

        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Global rule.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        primitives = _make_primitives(inst)
        base_result = _make_result(stats={})

        result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)

        out = self._output_path()
        self.assertFalse(out.exists())
        self.assertTrue(result.success)

    def test_oserror_reading_file_adds_error(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", resolve_links=False)

        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Global rule.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        primitives = _make_primitives(inst)
        base_result = _make_result(stats={})

        out = self._output_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("fake", encoding="utf-8")

        target_str = str(out)

        original_read_text = Path.read_text

        def _fake_read_text(self_path, *args, **kwargs):
            if str(self_path) == target_str:
                raise OSError("read denied")
            return original_read_text(self_path, *args, **kwargs)

        with patch("pathlib.Path.read_text", _fake_read_text):
            result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)

        self.assertFalse(result.success)
        self.assertTrue(any("read denied" in e or "Failed to read" in e for e in result.errors))

    def test_oserror_writing_file_adds_error(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(target="vscode", resolve_links=False, with_constitution=False)

        inst = _make_instruction(
            name="global",
            apply_to=None,
            content="Global rule.",
            file_path=Path(self.tmp) / "global.instructions.md",
        )
        primitives = _make_primitives(inst)
        base_result = _make_result(stats={})

        with patch("pathlib.Path.write_text", side_effect=OSError("write denied")):
            result = compiler._maybe_emit_copilot_root_instructions(config, primitives, base_result)

        self.assertFalse(result.success)
        self.assertTrue(any("write denied" in e or "Failed to write" in e for e in result.errors))


# ---------------------------------------------------------------------------
# AgentsCompiler._cleanup_copilot_root_instructions()
# ---------------------------------------------------------------------------


class TestCleanupCopilotRootInstructions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_does_not_exist_noop(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        out = Path(self.tmp) / ".github" / "copilot-instructions.md"
        base_result = _make_result(stats={})
        result = compiler._cleanup_copilot_root_instructions(out, base_result)
        self.assertIn("copilot_root_instructions_removed", result.stats)
        self.assertEqual(result.stats["copilot_root_instructions_removed"], 0)

    def test_hand_authored_file_not_removed(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        out = Path(self.tmp) / ".github" / "copilot-instructions.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# Hand authored, no marker\n", encoding="utf-8")

        base_result = _make_result(stats={})
        result = compiler._cleanup_copilot_root_instructions(out, base_result)

        self.assertTrue(out.exists())
        self.assertEqual(result.stats["copilot_root_instructions_removed"], 0)

    def test_apm_generated_file_removed(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        out = Path(self.tmp) / ".github" / "copilot-instructions.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"{_COPILOT_ROOT_GENERATED_MARKER}\n# Generated content\n",
            encoding="utf-8",
        )

        base_result = _make_result(stats={})
        result = compiler._cleanup_copilot_root_instructions(out, base_result)

        self.assertFalse(out.exists())
        self.assertEqual(result.stats["copilot_root_instructions_removed"], 1)

    def test_oserror_on_read_adds_error(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        out = Path(self.tmp) / ".github" / "copilot-instructions.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("content", encoding="utf-8")

        base_result = _make_result(stats={})
        with patch("pathlib.Path.read_text", side_effect=OSError("read error")):
            result = compiler._cleanup_copilot_root_instructions(out, base_result)

        self.assertFalse(result.success)
        self.assertTrue(any("read error" in e or "Failed to remove" in e for e in result.errors))


# ---------------------------------------------------------------------------
# AgentsCompiler.generate_output() — resolve_links=False
# ---------------------------------------------------------------------------


class TestGenerateOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_links_false_skips_link_resolution(self) -> None:
        from apm_cli.compilation.template_builder import TemplateData

        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(resolve_links=False)
        td = TemplateData(
            instructions_content="## Instructions\n", version="1.0", chatmode_content=None
        )

        with patch("apm_cli.compilation.agents_compiler.resolve_markdown_links") as mock_resolve:
            compiler.generate_output(td, config)

        mock_resolve.assert_not_called()

    def test_resolve_links_true_calls_resolver(self) -> None:
        from apm_cli.compilation.template_builder import TemplateData

        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(resolve_links=True)
        td = TemplateData(
            instructions_content="## Instructions\n", version="1.0", chatmode_content=None
        )

        with patch(
            "apm_cli.compilation.agents_compiler.resolve_markdown_links",
            return_value="resolved content",
        ) as mock_resolve:
            result = compiler.generate_output(td, config)

        mock_resolve.assert_called_once()
        self.assertEqual(result, "resolved content")


# ---------------------------------------------------------------------------
# AgentsCompiler._generate_template_data() — chatmode not found
# ---------------------------------------------------------------------------


class TestGenerateTemplateData(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chatmode_not_found_adds_warning(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(chatmode="nonexistent-chatmode")
        primitives = _make_primitives()

        compiler._generate_template_data(primitives, config)

        self.assertTrue(any("nonexistent-chatmode" in w for w in compiler.warnings))

    def test_no_chatmode_no_warning(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(chatmode=None)
        primitives = _make_primitives()

        compiler._generate_template_data(primitives, config)

        self.assertEqual(len(compiler.warnings), 0)


# ---------------------------------------------------------------------------
# AgentsCompiler._compile_stats()
# ---------------------------------------------------------------------------


class TestCompileStats(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stats_contain_expected_keys(self) -> None:
        from apm_cli.compilation.template_builder import TemplateData

        compiler = AgentsCompiler(self.tmp)
        primitives = _make_primitives(
            _make_instruction(file_path=Path(self.tmp) / "inst.instructions.md")
        )
        td = TemplateData(instructions_content="abc", version="1.0", chatmode_content=None)

        stats = compiler._compile_stats(primitives, td)

        self.assertIn("primitives_found", stats)
        self.assertIn("instructions", stats)
        self.assertIn("content_length", stats)
        self.assertIn("version", stats)

    def test_stats_count_instructions(self) -> None:
        from apm_cli.compilation.template_builder import TemplateData

        compiler = AgentsCompiler(self.tmp)
        primitives = _make_primitives(
            _make_instruction(name="a", file_path=Path(self.tmp) / "a.instructions.md"),
            _make_instruction(name="b", file_path=Path(self.tmp) / "b.instructions.md"),
        )
        td = TemplateData(instructions_content="x", version="1.0", chatmode_content=None)

        stats = compiler._compile_stats(primitives, td)

        self.assertEqual(stats["instructions"], 2)


# ---------------------------------------------------------------------------
# AgentsCompiler._display_placement_preview and _display_trace_info
# ---------------------------------------------------------------------------


class TestDisplayMethods(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_placement(self, path: Path, instructions_count: int = 1) -> MagicMock:
        p = MagicMock()
        p.agents_path = path
        p.instructions = [MagicMock() for _ in range(instructions_count)]
        p.coverage_patterns = {"**/*.py", "**/*.ts"}
        p.source_attribution = {"key": "source.md"}
        inst = MagicMock()
        inst.apply_to = "**/*.py"
        inst.file_path = path.parent / "inst.instructions.md"
        p.instructions = [inst]
        return p

    def _make_distributed_result(self, paths: list[Path]) -> MagicMock:
        result = MagicMock()
        result.placements = [self._make_placement(p) for p in paths]
        return result

    def test_display_placement_preview_calls_log(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        logger = MagicMock()
        compiler._logger = logger
        dr = self._make_distributed_result([Path(self.tmp) / "sub" / "AGENTS.md"])
        compiler._display_placement_preview(dr)
        logger.verbose_detail.assert_called()

    def test_display_trace_info_calls_log(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        logger = MagicMock()
        compiler._logger = logger
        dr = self._make_distributed_result([Path(self.tmp) / "sub" / "AGENTS.md"])
        primitives = _make_primitives()
        compiler._display_trace_info(dr, primitives)
        logger.verbose_detail.assert_called()

    def test_log_noop_when_no_logger(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        compiler._logger = None
        dr = self._make_distributed_result([Path(self.tmp) / "AGENTS.md"])
        # Should not raise
        compiler._display_placement_preview(dr)
        compiler._display_trace_info(dr, _make_primitives())


# ---------------------------------------------------------------------------
# AgentsCompiler.validate_primitives() — link validation warnings
# ---------------------------------------------------------------------------


class TestValidatePrimitivesLinkErrors(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_broken_link_adds_warning(self) -> None:
        compiler = AgentsCompiler(self.tmp)
        inst_path = Path(self.tmp) / "inst.instructions.md"
        inst = _make_instruction(
            content="See [missing](./nonexistent.md) for details.",
            file_path=inst_path,
        )
        primitives = _make_primitives(inst)
        errors = compiler.validate_primitives(primitives)
        # validate_primitives returns empty errors list (treats as warnings)
        self.assertEqual(errors, [])
        # Broken link should be recorded as warning
        self.assertTrue(len(compiler.warnings) >= 1)


# ---------------------------------------------------------------------------
# AgentsCompiler._log() — delegates when logger wired
# ---------------------------------------------------------------------------


class TestLogDelegation(unittest.TestCase):
    def test_log_delegates_to_logger(self) -> None:
        compiler = AgentsCompiler("/tmp")
        logger = MagicMock()
        compiler._logger = logger
        compiler._log("progress", "test message", symbol="info")
        logger.progress.assert_called_once_with("test message", symbol="info")

    def test_log_noop_when_no_logger(self) -> None:
        compiler = AgentsCompiler("/tmp")
        compiler._logger = None
        # Should not raise
        compiler._log("progress", "test")


# ---------------------------------------------------------------------------
# compile_agents_md() — vscode alias
# ---------------------------------------------------------------------------


class TestCompileAgentsMdVscodeAlias(unittest.TestCase):
    def test_vscode_alias_routes_to_agents_compile(self) -> None:
        primitives = _make_primitives()
        good_result = CompilationResult(
            success=True,
            output_path="AGENTS.md",
            content="# Generated",
            warnings=[],
            errors=[],
            stats={},
        )
        with patch(
            "apm_cli.compilation.agents_compiler.AgentsCompiler.compile",
            return_value=good_result,
        ) as mock_compile:
            compile_agents_md(primitives=primitives)

        mock_compile.assert_called_once()
        config_arg = mock_compile.call_args[0][0]
        self.assertEqual(config_arg.strategy, "single-file")


# ---------------------------------------------------------------------------
# CompilationConfig.from_apm_yml() — exclude as string
# ---------------------------------------------------------------------------


class TestCompilationConfigFromApmYmlExcludeString(unittest.TestCase):
    def setUp(self) -> None:
        self.original_dir = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self) -> None:
        import shutil

        os.chdir(self.original_dir)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_apm_yml(self, data: dict) -> None:
        import yaml

        with open("apm.yml", "w") as f:
            yaml.dump(data, f)

    def test_exclude_as_string_becomes_list(self) -> None:
        self._write_apm_yml({"compilation": {"exclude": "node_modules/**"}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.exclude, ["node_modules/**"])

    def test_exclude_as_list_preserved(self) -> None:
        self._write_apm_yml({"compilation": {"exclude": ["node_modules/**", "dist/**"]}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.exclude, ["node_modules/**", "dist/**"])

    def test_override_single_agents_sets_strategy(self) -> None:
        self._write_apm_yml({})
        config = CompilationConfig.from_apm_yml(single_agents=True)
        self.assertEqual(config.strategy, "single-file")

    def test_exception_in_loading_falls_back_to_defaults(self) -> None:
        # Write invalid YAML
        with open("apm.yml", "w") as f:
            f.write("{{invalid yaml{{")
        config = CompilationConfig.from_apm_yml()
        # Should fall back to defaults without raising
        self.assertEqual(config.output_path, "AGENTS.md")


if __name__ == "__main__":
    unittest.main()
