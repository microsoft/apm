from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ..utils.constitution_fixtures import temp_project_with_constitution, DEFAULT_CONSTITUTION

CLI = [sys.executable, "-m", "apm_cli.cli", "compile"]


def test_copilot_instructions_file_generation():
    """Test that .github/copilot-instructions.md is generated during compilation."""
    with temp_project_with_constitution(constitution_text=DEFAULT_CONSTITUTION) as proj:
        # Run compilation
        proc = subprocess.run(CLI, cwd=str(proj), capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0
        
        # Check that .github/copilot-instructions.md was created
        copilot_instructions_path = proj / ".github" / "copilot-instructions.md"
        assert copilot_instructions_path.exists()
        
        # Check that it contains expected content (even if minimal)
        content = copilot_instructions_path.read_text(encoding="utf-8")
        assert len(content) > 0


def test_copilot_instructions_with_contributing_file():
    """Test that .github/copilot-instructions.md uses content from .apm/instructions/contributing.md if it exists."""
    with temp_project_with_constitution(constitution_text=DEFAULT_CONSTITUTION) as proj:
        # Create a contributing.md file with specific content
        contributing_path = proj / ".apm" / "instructions" / "contributing.md"
        contributing_path.parent.mkdir(parents=True, exist_ok=True)
        contributing_path.write_text("# Custom Contributing Guidelines\n\nThis is custom content.", encoding="utf-8")
        
        # Run compilation
        proc = subprocess.run(CLI, cwd=str(proj), capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0
        
        # Check that .github/copilot-instructions.md was created with custom content
        copilot_instructions_path = proj / ".github" / "copilot-instructions.md"
        assert copilot_instructions_path.exists()
        
        content = copilot_instructions_path.read_text(encoding="utf-8")
        assert "Custom Contributing Guidelines" in content