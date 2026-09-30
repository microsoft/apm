"""Integration: install -> compile deduplication for Copilot (issues #1550 and #3099).

Pins the user-visible promise across the install and compile boundaries:
after ``apm install --target copilot`` populates ``.github/instructions/``
with per-instruction files, a subsequent ``apm compile --target agents``
must omit the instructions content from ``AGENTS.md`` and matching global
instructions from ``copilot-instructions.md`` so Copilot does not load
duplicate content into its context window.

This is the parity guard for #1550 (sibling of the Claude dedup in #1445).
The unit suite covers the compiler logic in isolation; this regression trap
fires if a future refactor of ``apm install``'s write path silently breaks
the filesystem contract that ``apm compile`` reads (any ``.md`` file under
``.github/instructions/`` triggers the skip).

Both stages run through the real CLI as subprocesses to exercise the full
code path, not just the unit logic.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

APM_YML = """name: test-copilot-dedup
version: 1.0.0
description: Install->compile dedup regression trap for Copilot/AGENTS.md
author: Test
targets:
  - copilot
"""

INSTRUCTION_BODY = (
    "---\n"
    "description: Style rule for the Copilot dedup test\n"
    "applyTo: '**/*.py'\n"
    "---\n"
    "# Style rule\n"
    "Use type hints everywhere.\n"
)

INSTRUCTION_SENTINEL = "Use type hints everywhere."
GLOBAL_INSTRUCTION_SENTINEL = "Use shared configuration for all repository changes."


def _run(apm_binary_path: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm_binary_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project_with_instruction():
    with tempfile.TemporaryDirectory(prefix="apm-copilot-dedup-e2e-") as tmp:
        proj = Path(tmp).resolve()
        (proj / "apm.yml").write_text(APM_YML, encoding="utf-8")
        instr_dir = proj / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "style.instructions.md").write_text(INSTRUCTION_BODY, encoding="utf-8")
        yield proj


@pytest.fixture
def project_with_global_instruction(tmp_path: Path):
    """Create a Copilot project containing one unscoped local instruction."""
    (tmp_path / "apm.yml").write_text(APM_YML, encoding="utf-8")
    instr_dir = tmp_path / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "global.instructions.md").write_text(
        "---\n"
        "description: Global Copilot guidance for the dedup test\n"
        "---\n"
        "# Shared configuration\n"
        f"{GLOBAL_INSTRUCTION_SENTINEL}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.integration
def test_install_then_compile_skips_duplicated_instructions(
    project_with_instruction, apm_binary_path: Path
):
    """After install populates .github/instructions/, compile must drop the
    instructions content from AGENTS.md. Pre-fix, the content was duplicated
    into both files on every compile.
    """
    proj = project_with_instruction

    install_res = _run(apm_binary_path, proj, "install", "--target", "copilot")
    assert install_res.returncode == 0, (
        f"install stdout:\n{install_res.stdout}\ninstall stderr:\n{install_res.stderr}"
    )
    instructions_dir = proj / ".github" / "instructions"
    assert instructions_dir.exists(), (
        "install --target copilot must populate .github/instructions/ "
        "(this is the precondition the dedup logic reads)"
    )
    instr_files = sorted(instructions_dir.glob("*.md"))
    assert instr_files, "install must emit at least one *.md instructions file"

    compile_res = _run(apm_binary_path, proj, "compile", "--target", "agents")
    assert compile_res.returncode == 0, (
        f"compile stdout:\n{compile_res.stdout}\ncompile stderr:\n{compile_res.stderr}"
    )

    agents_files = sorted(proj.rglob("AGENTS.md"))
    assert not agents_files, (
        "AGENTS.md must be suppressed entirely when install already populated "
        f".github/instructions/. Found: {agents_files}"
    )
    assert "AGENTS.md not generated" in compile_res.stdout


@pytest.mark.integration
def test_compile_without_github_instructions_includes_content(
    project_with_instruction, apm_binary_path: Path
):
    """Without .github/instructions/ populated, compile should include
    instruction content in AGENTS.md (the non-dedup baseline).
    """
    proj = project_with_instruction

    compile_res = _run(apm_binary_path, proj, "compile", "--target", "agents")
    assert compile_res.returncode == 0, (
        f"compile stdout:\n{compile_res.stdout}\ncompile stderr:\n{compile_res.stderr}"
    )

    agents_files = sorted(proj.rglob("AGENTS.md"))
    assert agents_files, "compile --target agents must generate at least one AGENTS.md"

    all_content = "\n".join(f.read_text(encoding="utf-8") for f in agents_files)
    assert INSTRUCTION_SENTINEL in all_content, (
        "Without .github/instructions/ populated, AGENTS.md must include "
        "the instruction content. Content was:\n" + all_content
    )


@pytest.mark.integration
def test_install_then_compile_deduplicates_global_copilot_root_and_force_restores_it(
    project_with_global_instruction: Path,
    apm_binary_path: Path,
):
    """Installed no-applyTo rules stay out of the root file unless forced."""
    proj = project_with_global_instruction

    install_res = _run(apm_binary_path, proj, "install", "--target", "copilot")
    assert install_res.returncode == 0, (
        f"install stdout:\n{install_res.stdout}\ninstall stderr:\n{install_res.stderr}"
    )
    installed_rule = proj / ".github" / "instructions" / "global.instructions.md"
    assert installed_rule.exists(), "install must deploy the no-applyTo Copilot instruction"
    assert GLOBAL_INSTRUCTION_SENTINEL in installed_rule.read_text(encoding="utf-8")

    compile_res = _run(apm_binary_path, proj, "compile", "--target", "copilot")
    assert compile_res.returncode == 0, (
        f"compile stdout:\n{compile_res.stdout}\ncompile stderr:\n{compile_res.stderr}"
    )
    root_file = proj / ".github" / "copilot-instructions.md"
    assert not root_file.exists(), (
        "default Copilot compile must not repeat an installed global instruction "
        "in copilot-instructions.md"
    )

    forced_compile = _run(
        apm_binary_path,
        proj,
        "compile",
        "--target",
        "copilot",
        "--force-instructions",
    )
    assert forced_compile.returncode == 0, (
        f"forced compile stdout:\n{forced_compile.stdout}\n"
        f"forced compile stderr:\n{forced_compile.stderr}"
    )
    assert root_file.exists(), "--force-instructions must restore the aggregated root file"
    assert GLOBAL_INSTRUCTION_SENTINEL in root_file.read_text(encoding="utf-8")
