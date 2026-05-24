"""End-to-end test for comma-separated applyTo handling (issue #1366).

Builds a single instruction primitive with a comma-separated ``applyTo``
glob list and exercises each of the four target converters
(Copilot / Cursor / Windsurf / Claude), then asserts every segment ends
up in the rendered artifact in the target's native form.

Copilot must preserve the value verbatim (consuming tool splits it);
the other three must emit a YAML list under their respective key.
"""

import tempfile
from pathlib import Path

import pytest

from apm_cli.integration.instruction_integrator import InstructionIntegrator

COMMA_APPLY_TO = "**/src/**,**/api/**,**/services/**"
SEGMENTS = ["**/src/**", "**/api/**", "**/services/**"]


@pytest.fixture
def source_instruction():
    """Write a primitive instruction file with comma-separated applyTo."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "multi.instructions.md"
        src.write_text(
            "---\n"
            f"applyTo: '{COMMA_APPLY_TO}'\n"
            "description: 'rules for src api services'\n"
            "---\n"
            "\n"
            "# Multi-glob rules\n"
            "\n"
            "Body content.\n"
        )
        yield src


def test_copilot_preserves_verbatim(source_instruction, tmp_path):
    """Copilot target must keep the comma-list as-is."""
    dst = tmp_path / "copilot.instructions.md"
    integrator = InstructionIntegrator()
    integrator.copy_instruction(source_instruction, dst)
    out = dst.read_text()
    assert f"applyTo: '{COMMA_APPLY_TO}'" in out


def test_cursor_emits_yaml_list(source_instruction, tmp_path):
    dst = tmp_path / "cursor.mdc"
    integrator = InstructionIntegrator()
    integrator.copy_instruction_cursor(source_instruction, dst)
    out = dst.read_text()
    assert "globs:" in out
    for seg in SEGMENTS:
        assert f'  - "{seg}"' in out
    # Make sure we did NOT emit the legacy literal comma string.
    assert f'globs: "{COMMA_APPLY_TO}"' not in out


def test_windsurf_emits_yaml_list(source_instruction, tmp_path):
    dst = tmp_path / "windsurf.md"
    integrator = InstructionIntegrator()
    integrator.copy_instruction_windsurf(source_instruction, dst)
    out = dst.read_text()
    assert "trigger: glob" in out
    assert "globs:" in out
    for seg in SEGMENTS:
        assert f'  - "{seg}"' in out
    assert f'globs: "{COMMA_APPLY_TO}"' not in out


def test_claude_emits_yaml_list(source_instruction, tmp_path):
    dst = tmp_path / "claude.md"
    integrator = InstructionIntegrator()
    integrator.copy_instruction_claude(source_instruction, dst)
    out = dst.read_text()
    assert "paths:" in out
    for seg in SEGMENTS:
        assert f'  - "{seg}"' in out
    assert f'paths: "{COMMA_APPLY_TO}"' not in out
