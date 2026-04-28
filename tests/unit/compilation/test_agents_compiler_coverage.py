"""Additional unit tests for agents_compiler.py to improve coverage.

Targets the uncovered branches and methods not exercised by test_compilation.py:
- CompilationConfig.from_apm_yml() additional fields (target, strategy, single_file,
  placement, source_attribution) and exception handling
- AgentsCompiler.compile() exception handling
- AgentsCompiler.validate_primitives() with errors and link errors
- AgentsCompiler._write_distributed_file()
- AgentsCompiler._generate_placement_summary() and _generate_distributed_summary()
- compile_agents_md() error path
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import yaml

from apm_cli.compilation.agents_compiler import (
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
    name="test", apply_to="**/*.py", content="Use type hints.", file_path=None
):
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


def _make_primitives(*instructions):
    col = PrimitiveCollection()
    for inst in instructions:
        col.add_primitive(inst)
    return col


# ---------------------------------------------------------------------------
# CompilationConfig.from_apm_yml() – additional fields
# ---------------------------------------------------------------------------


class TestCompilationConfigFromApmYmlAdditional(unittest.TestCase):
    """Test CompilationConfig.from_apm_yml() loading of less-tested fields."""

    def setUp(self):
        self.original_dir = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.original_dir)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_apm_yml(self, data):
        with open("apm.yml", "w") as f:
            yaml.dump(data, f)

    def test_from_apm_yml_target_field(self):
        """from_apm_yml reads 'target' field from compilation section."""
        self._write_apm_yml({"compilation": {"target": "claude"}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.target, "claude")

    def test_from_apm_yml_strategy_field(self):
        """from_apm_yml reads 'strategy' field."""
        self._write_apm_yml({"compilation": {"strategy": "single-file"}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.strategy, "single-file")

    def test_from_apm_yml_single_file_legacy_true(self):
        """from_apm_yml legacy 'single_file: true' sets strategy to single-file."""
        self._write_apm_yml({"compilation": {"single_file": True}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.strategy, "single-file")
        self.assertTrue(config.single_agents)

    def test_from_apm_yml_single_file_legacy_false(self):
        """from_apm_yml legacy 'single_file: false' leaves strategy as default."""
        self._write_apm_yml({"compilation": {"single_file": False}})
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.strategy, "distributed")

    def test_from_apm_yml_min_instructions_per_file(self):
        """from_apm_yml reads placement.min_instructions_per_file."""
        self._write_apm_yml(
            {"compilation": {"placement": {"min_instructions_per_file": 3}}}
        )
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.min_instructions_per_file, 3)

    def test_from_apm_yml_source_attribution(self):
        """from_apm_yml reads source_attribution field."""
        self._write_apm_yml({"compilation": {"source_attribution": False}})
        config = CompilationConfig.from_apm_yml()
        self.assertFalse(config.source_attribution)

    def test_from_apm_yml_exception_falls_back_to_defaults(self):
        """from_apm_yml returns defaults when config loading raises an exception."""
        # Write a file that will trigger a YAML parsing error.
        with open("apm.yml", "w") as f:
            f.write("compilation: [invalid: yaml: {{")
        config = CompilationConfig.from_apm_yml()
        # Should still return a valid config with defaults.
        self.assertIsInstance(config, CompilationConfig)
        self.assertEqual(config.output_path, "AGENTS.md")

    def test_from_apm_yml_no_file_returns_defaults(self):
        """from_apm_yml returns defaults when apm.yml does not exist."""
        config = CompilationConfig.from_apm_yml()
        self.assertEqual(config.output_path, "AGENTS.md")
        self.assertEqual(config.strategy, "distributed")

    def test_from_apm_yml_override_single_agents_sets_strategy(self):
        """from_apm_yml override single_agents=True forces strategy=single-file."""
        config = CompilationConfig.from_apm_yml(single_agents=True)
        self.assertEqual(config.strategy, "single-file")

    def test_from_apm_yml_override_none_is_ignored(self):
        """from_apm_yml override with value=None does not override config."""
        self._write_apm_yml({"compilation": {"target": "claude"}})
        config = CompilationConfig.from_apm_yml(target=None)
        # None override is skipped; config file value remains.
        self.assertEqual(config.target, "claude")


# ---------------------------------------------------------------------------
# CompilationConfig.__post_init__
# ---------------------------------------------------------------------------


class TestCompilationConfigPostInit(unittest.TestCase):

    def test_single_agents_sets_strategy(self):
        config = CompilationConfig(single_agents=True)
        self.assertEqual(config.strategy, "single-file")

    def test_exclude_none_initialised_to_empty_list(self):
        config = CompilationConfig(exclude=None)
        self.assertEqual(config.exclude, [])


# ---------------------------------------------------------------------------
# AgentsCompiler.compile() – exception path
# ---------------------------------------------------------------------------


class TestAgentsCompilerCompileException(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compile_returns_failure_on_exception(self):
        """compile() catches unexpected exceptions and returns failure result."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(strategy="single-file", dry_run=True)
        primitives = _make_primitives()

        # Patch _compile_single_file to raise.
        with patch.object(
            compiler, "_compile_single_file", side_effect=RuntimeError("boom")
        ):
            result = compiler.compile(config, primitives)

        self.assertFalse(result.success)
        self.assertTrue(any("boom" in e for e in result.errors))

    def test_compile_local_only_calls_basic_discover(self):
        """compile() with local_only uses basic discover_primitives."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(
            strategy="single-file", local_only=True, dry_run=True
        )
        primitives = _make_primitives()

        with patch(
            "apm_cli.compilation.agents_compiler.discover_primitives",
            return_value=primitives,
        ) as mock_disc:
            result = compiler.compile(config)  # no primitives passed → discovers

        mock_disc.assert_called_once_with(
            str(compiler.base_dir), exclude_patterns=config.exclude
        )


# ---------------------------------------------------------------------------
# AgentsCompiler.validate_primitives() – error branches
# ---------------------------------------------------------------------------


class TestValidatePrimitivesErrors(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validate_primitives_adds_warnings_for_primitive_errors(self):
        """validate_primitives converts primitive errors into warnings."""
        compiler = AgentsCompiler(self.tmp)

        bad_instruction = _make_instruction(
            file_path=Path(self.tmp) / "bad.instructions.md"
        )
        # Make validate() return errors.
        bad_instruction.validate = MagicMock(
            return_value=["Missing required field 'name'"]
        )

        primitives = _make_primitives(bad_instruction)
        errors = compiler.validate_primitives(primitives)

        self.assertEqual(errors, [])  # errors list is always empty
        self.assertEqual(len(compiler.warnings), 1)
        self.assertIn("Missing required field 'name'", compiler.warnings[0])

    def test_validate_primitives_outside_base_dir_uses_absolute_path(self):
        """validate_primitives falls back to absolute path if file is outside base_dir."""
        tmp2 = tempfile.mkdtemp()
        try:
            compiler = AgentsCompiler(self.tmp)

            # Instruction file is in a DIFFERENT tmp dir → outside base_dir.
            inst = _make_instruction(file_path=Path(tmp2) / "out.instructions.md")
            inst.validate = MagicMock(return_value=["some error"])

            primitives = _make_primitives(inst)
            compiler.validate_primitives(primitives)

            self.assertEqual(len(compiler.warnings), 1)
            # portable_relpath resolves and returns POSIX paths
            resolved_tmp2 = Path(tmp2).resolve().as_posix()
            self.assertIn(resolved_tmp2, compiler.warnings[0])
        finally:
            import shutil

            shutil.rmtree(tmp2, ignore_errors=True)

    def test_validate_primitives_link_errors_added_as_warnings(self):
        """validate_primitives adds link-validation errors as warnings."""
        compiler = AgentsCompiler(self.tmp)

        inst = _make_instruction(
            content="See [missing link](nonexistent-file.md)",
            file_path=Path(self.tmp) / "inst.instructions.md",
        )
        primitives = _make_primitives(inst)
        compiler.validate_primitives(primitives)

        # Broken link → at least one warning.
        self.assertGreaterEqual(len(compiler.warnings), 1)
        warning_text = " ".join(compiler.warnings)
        self.assertIn("nonexistent-file.md", warning_text)

    def test_validate_primitives_link_errors_outside_base_dir(self):
        """validate_primitives uses absolute path for link errors outside base_dir."""
        tmp2 = tempfile.mkdtemp()
        try:
            compiler = AgentsCompiler(self.tmp)

            inst = _make_instruction(
                content="[broken](nowhere.md)",
                file_path=Path(tmp2) / "inst.instructions.md",
            )
            primitives = _make_primitives(inst)
            compiler.validate_primitives(primitives)

            self.assertGreaterEqual(len(compiler.warnings), 1)
        finally:
            import shutil

            shutil.rmtree(tmp2, ignore_errors=True)


# ---------------------------------------------------------------------------
# AgentsCompiler._write_output_file() – error path
# ---------------------------------------------------------------------------


class TestWriteOutputFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_output_file_oserror_adds_error(self):
        """_write_output_file adds error message when OS error occurs."""
        compiler = AgentsCompiler(self.tmp)
        bad_path = str(Path(self.tmp) / "nodir" / "deep" / "AGENTS.md")

        # Don't create parent directory so the write fails.
        compiler._write_output_file(bad_path, "content")
        self.assertEqual(len(compiler.errors), 1)
        self.assertIn("Failed to write", compiler.errors[0])


# ---------------------------------------------------------------------------
# AgentsCompiler._write_distributed_file()
# ---------------------------------------------------------------------------


class TestWriteDistributedFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_distributed_file_creates_dir_and_writes(self):
        """_write_distributed_file creates parent dir and writes content."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(with_constitution=False)
        target = Path(self.tmp) / "sub" / "AGENTS.md"

        compiler._write_distributed_file(target, "# Hello\n", config)

        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "# Hello\n")

    def test_write_distributed_file_no_constitution(self):
        """_write_distributed_file skips constitution injection when disabled."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(with_constitution=False)
        target = Path(self.tmp) / "AGENTS.md"

        compiler._write_distributed_file(target, "content", config)

        self.assertEqual(target.read_text(), "content")

    def test_write_distributed_file_constitution_exception_falls_back(self):
        """_write_distributed_file uses original content when injection fails."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(with_constitution=True)
        target = Path(self.tmp) / "AGENTS.md"

        with patch(
            "apm_cli.compilation.agents_compiler.AgentsCompiler._write_distributed_file",
            wraps=compiler._write_distributed_file,
        ):
            with patch(
                "apm_cli.compilation.injector.ConstitutionInjector.inject",
                side_effect=RuntimeError("injection error"),
            ):
                compiler._write_distributed_file(target, "original content", config)

        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "original content")

    def test_write_distributed_file_raises_oserror_on_permission(self):
        """_write_distributed_file re-raises OSError."""
        compiler = AgentsCompiler(self.tmp)
        config = CompilationConfig(with_constitution=False)
        # Target inside a non-existent nested directory that cannot be created.
        with patch("pathlib.Path.mkdir", side_effect=OSError("permission denied")):
            with self.assertRaises(OSError):
                target = Path(self.tmp) / "no" / "way" / "AGENTS.md"
                compiler._write_distributed_file(target, "x", config)


# ---------------------------------------------------------------------------
# AgentsCompiler._generate_placement_summary() and _generate_distributed_summary()
# ---------------------------------------------------------------------------


class TestGenerateSummaries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_distributed_result(self, paths, stats=None):
        """Build a minimal mock DistributedCompilationResult."""
        result = MagicMock()
        result.success = True
        result.warnings = []
        result.errors = []
        result.content_map = {}
        result.stats = stats or {
            "total_instructions_placed": 3,
            "total_patterns_covered": 2,
        }

        placements = []
        for path, n_instructions in paths:
            p = MagicMock()
            p.agents_path = Path(path)
            p.instructions = [MagicMock()] * n_instructions
            p.coverage_patterns = {"**/*.py", "**/*.ts"}
            p.source_attribution = {}
            placements.append(p)
        result.placements = placements
        return result

    def test_generate_placement_summary_contains_paths(self):
        """_generate_placement_summary includes relative paths and instruction counts."""
        compiler = AgentsCompiler(self.tmp)
        sub_path = str(Path(self.tmp) / "sub" / "AGENTS.md")
        result = self._make_distributed_result([(sub_path, 5)])

        summary = compiler._generate_placement_summary(result)
        self.assertIn("sub/AGENTS.md", summary)
        self.assertIn("5", summary)

    def test_generate_placement_summary_outside_base_uses_absolute(self):
        """_generate_placement_summary falls back to absolute path."""
        other_tmp = tempfile.mkdtemp()
        try:
            compiler = AgentsCompiler(self.tmp)
            outside_path = str(Path(other_tmp) / "AGENTS.md")
            result = self._make_distributed_result([(outside_path, 2)])

            summary = compiler._generate_placement_summary(result)
            # portable_relpath resolves and returns POSIX paths
            resolved_path = (Path(other_tmp) / "AGENTS.md").resolve().as_posix()
            self.assertIn(resolved_path, summary)
        finally:
            import shutil

            shutil.rmtree(other_tmp, ignore_errors=True)

    def test_generate_distributed_summary_format(self):
        """_generate_distributed_summary generates a human-readable summary."""
        compiler = AgentsCompiler(self.tmp)
        sub_path = str(Path(self.tmp) / "src" / "AGENTS.md")
        result = self._make_distributed_result([(sub_path, 3)])

        summary = compiler._generate_distributed_summary(result, CompilationConfig())
        self.assertIn("Distributed AGENTS.md Compilation Summary", summary)
        self.assertIn("src/AGENTS.md", summary)
        self.assertIn("1 AGENTS.md files", summary)
        self.assertIn("--single-agents", summary)

    def test_generate_distributed_summary_outside_base(self):
        """_generate_distributed_summary falls back to absolute path outside base_dir."""
        other_tmp = tempfile.mkdtemp()
        try:
            compiler = AgentsCompiler(self.tmp)
            outside_path = str(Path(other_tmp) / "AGENTS.md")
            result = self._make_distributed_result([(outside_path, 1)])

            summary = compiler._generate_distributed_summary(
                result, CompilationConfig()
            )
            # portable_relpath resolves and returns POSIX paths
            resolved_path = (Path(other_tmp) / "AGENTS.md").resolve().as_posix()
            self.assertIn(resolved_path, summary)
        finally:
            import shutil

            shutil.rmtree(other_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# AgentsCompiler._merge_results()
# ---------------------------------------------------------------------------


class TestMergeResults(unittest.TestCase):

    def _make_result(
        self,
        success=True,
        output_path="out",
        content="c",
        warnings=None,
        errors=None,
        stats=None,
    ):
        return CompilationResult(
            success=success,
            output_path=output_path,
            content=content,
            warnings=warnings or [],
            errors=errors or [],
            stats=stats or {},
        )

    def test_merge_empty_list_returns_success(self):
        compiler = AgentsCompiler("/tmp")
        result = compiler._merge_results([])
        self.assertTrue(result.success)
        self.assertEqual(result.content, "")

    def test_merge_single_result_passes_through(self):
        compiler = AgentsCompiler("/tmp")
        r = self._make_result(output_path="custom.md", content="hello")
        merged = compiler._merge_results([r])
        self.assertIs(merged, r)

    def test_merge_two_results_combines_content(self):
        compiler = AgentsCompiler("/tmp")
        r1 = self._make_result(
            content="part1", output_path="a.md", warnings=["w1"], stats={"count": 2}
        )
        r2 = self._make_result(
            content="part2", output_path="b.md", warnings=["w2"], stats={"count": 3}
        )
        merged = compiler._merge_results([r1, r2])
        self.assertIn("part1", merged.content)
        self.assertIn("part2", merged.content)
        self.assertIn("w1", merged.warnings)
        self.assertIn("w2", merged.warnings)
        self.assertEqual(merged.stats["count"], 5)

    def test_merge_any_failure_propagates(self):
        compiler = AgentsCompiler("/tmp")
        r1 = self._make_result(success=True)
        r2 = self._make_result(success=False)
        merged = compiler._merge_results([r1, r2])
        self.assertFalse(merged.success)

    def test_merge_empty_paths_excluded(self):
        compiler = AgentsCompiler("/tmp")
        r1 = self._make_result(output_path="", content="")
        r2 = self._make_result(output_path="b.md", content="")
        merged = compiler._merge_results([r1, r2])
        self.assertEqual(merged.output_path, "b.md")


# ---------------------------------------------------------------------------
# compile_agents_md() convenience function – error path
# ---------------------------------------------------------------------------


class TestCompileAgentsMdFunction(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.original_dir = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.original_dir)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compile_agents_md_raises_on_failure(self):
        """compile_agents_md raises RuntimeError when compilation fails."""
        primitives = _make_primitives()
        bad_result = CompilationResult(
            success=False,
            output_path="",
            content="",
            warnings=[],
            errors=["test failure"],
            stats={},
        )

        with patch(
            "apm_cli.compilation.agents_compiler.AgentsCompiler.compile",
            return_value=bad_result,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                compile_agents_md(primitives=primitives)

        self.assertIn("test failure", str(ctx.exception))

    def test_compile_agents_md_returns_content_on_success(self):
        """compile_agents_md returns content string on success."""
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
        ):
            content = compile_agents_md(primitives=primitives)

        self.assertEqual(content, "# Generated")


# ---------------------------------------------------------------------------
# _compile_claude_md – constitution injection failure path (G2)
# ---------------------------------------------------------------------------


class TestCompileClaudeMdConstitutionInjectionFailure(unittest.TestCase):
    """Verify that ConstitutionInjector.inject failure inside _compile_claude_md
    is swallowed and logged, matching the symmetric AGENTS.md behaviour."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Create a minimal instruction so compilation has something to work with
        inst_dir = Path(self.tmp) / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        inst_file = inst_dir / "test.instructions.md"
        inst_file.write_text(
            "---\ndescription: test\napplyTo: '**/*.py'\n---\nUse type hints.\n"
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compile_claude_md_constitution_injection_failure(self):
        """Constitution injection failure in _compile_claude_md is swallowed
        and logged at debug level, compilation still succeeds."""
        compiler = AgentsCompiler(self.tmp)
        primitives = _make_primitives(
            _make_instruction(
                name="style",
                apply_to="**/*.py",
                content="Use type hints.",
                file_path=Path(self.tmp) / ".apm" / "instructions" / "style.instructions.md",
            )
        )
        config = CompilationConfig(
            target="claude",
            with_constitution=True,
            dry_run=False,
        )

        with patch(
            "apm_cli.compilation.injector.ConstitutionInjector.inject",
            side_effect=RuntimeError("injector exploded"),
        ), patch(
            "apm_cli.compilation.agents_compiler._logger"
        ) as mock_logger:
            result = compiler._compile_claude_md(config, primitives)

        # Compilation must still succeed (the exception is swallowed)
        self.assertTrue(
            result.success,
            f"Expected successful compilation, got errors: {result.errors}",
        )

        # Verify the debug log was emitted with the expected message fragment
        debug_calls = mock_logger.debug.call_args_list
        matched = any(
            "Constitution injection failed" in str(call)
            for call in debug_calls
        )
        self.assertTrue(
            matched,
            f"Expected 'Constitution injection failed' in debug logs, got: {debug_calls}",
        )


if __name__ == "__main__":
    unittest.main()
