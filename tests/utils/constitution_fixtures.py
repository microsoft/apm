from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Callable  # noqa: F401
from pathlib import Path


@contextlib.contextmanager
def temp_project_with_constitution(base: Path | None = None, constitution_text: str | None = None):
    """Create a temp project directory containing optional constitution.

    Yields project path. Caller cleans nothing; context manager handles removal.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="apm-constitution-"))
    try:
        if base:
            for item in base.iterdir():
                target = tmp_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)

        # Create apm.yml file to make this an APM project
        apm_yml_content = """name: test-project
version: 1.0.0
description: Test project for constitution tests
author: Test Author

scripts:
  start: "echo 'test script'"
"""
        (tmp_dir / "apm.yml").write_text(apm_yml_content, encoding="utf-8")

        # Only create APM content if we're testing constitution functionality
        # When constitution_text is None, we want to test the case with no content
        if constitution_text is not None:
            # Create minimal APM content so the CLI has something to compile
            apm_dir = tmp_dir / ".apm" / "instructions"
            apm_dir.mkdir(parents=True, exist_ok=True)

            # Create a minimal instruction file
            instruction_content = """---
description: Test instruction for compilation
applyTo: "**/*.md"
---

# Test Instruction

This is a test instruction to ensure the CLI has APM content to compile.
"""
            (apm_dir / "test.instructions.md").write_text(instruction_content, encoding="utf-8")

        # Create constitution in the correct .specify/memory/ directory
        mem_dir = tmp_dir / ".specify" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        if constitution_text is not None:
            (mem_dir / "constitution.md").write_text(constitution_text, encoding="utf-8")
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


DEFAULT_CONSTITUTION = (
    """# Project Constitution\n\nShip Fast.\nTest First.\nDocumentation Must Track Code.\n"""
)
