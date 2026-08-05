"""Regression coverage for target-independent compile placement analysis."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction, PrimitiveCollection

pytestmark = pytest.mark.component


def _write_project(project: Path) -> None:
    """Create a minimal project with one scoped instruction."""
    (project / ".apm" / "instructions").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "apm.yml").write_text(
        "name: target-scaling\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    (project / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (project / ".apm" / "instructions" / "python.instructions.md").write_text(
        '---\napplyTo: "**/*.py"\n---\nUse type hints.\n',
        encoding="utf-8",
    )


def test_multi_target_compile_runs_placement_once(tmp_path: Path) -> None:
    """Adding target projections must not multiply placement analysis."""
    _write_project(tmp_path)
    runner = CliRunner()
    original = ContextOptimizer.optimize_instruction_placement
    calls_by_target: dict[str, int] = {}

    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        for target in ("copilot", "claude,copilot,opencode"):
            call_count = 0

            def counting_optimize(self, *args, **kwargs):
                nonlocal call_count
                call_count += 1
                return original(self, *args, **kwargs)

            with patch.object(
                ContextOptimizer,
                "optimize_instruction_placement",
                counting_optimize,
            ):
                result = runner.invoke(
                    cli,
                    [
                        "compile",
                        "--dry-run",
                        "--no-links",
                        "--target",
                        target,
                    ],
                )

            assert result.exit_code == 0, result.output
            calls_by_target[target] = call_count
    finally:
        os.chdir(old_cwd)

    assert calls_by_target == {
        "copilot": 1,
        "claude,copilot,opencode": 1,
    }


def test_placement_cache_is_scoped_to_one_compile_invocation(tmp_path: Path) -> None:
    """Reusing an AgentsCompiler must analyze each new primitive snapshot."""
    compiler = AgentsCompiler(str(tmp_path))
    config = CompilationConfig(target="all", dry_run=True)
    optimize_calls = 0
    original = ContextOptimizer.optimize_instruction_placement

    def counting_optimize(self, *args, **kwargs):
        nonlocal optimize_calls
        optimize_calls += 1
        return original(self, *args, **kwargs)

    with patch.object(
        ContextOptimizer,
        "optimize_instruction_placement",
        counting_optimize,
    ):
        for suffix in ("py", "js"):
            source = tmp_path / "src" / f"main.{suffix}"
            source.parent.mkdir(exist_ok=True)
            source.touch()
            primitives = PrimitiveCollection()
            primitives.add_primitive(
                Instruction(
                    name=f"{suffix}-style",
                    file_path=tmp_path / f"{suffix}.instructions.md",
                    description=f"{suffix} instructions",
                    apply_to=f"**/*.{suffix}",
                    content=f"Use {suffix} conventions.",
                )
            )
            result = compiler.compile(config, primitives)
            assert result.success

    assert optimize_calls == 2
