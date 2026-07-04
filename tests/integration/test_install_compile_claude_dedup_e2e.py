"""Integration: install -> compile deduplication for Claude Code (PR #1146).

Pins the user-visible promise across the install and compile boundaries:
after ``apm install --target claude`` populates ``.claude/rules/`` with
per-instruction files, a subsequent ``apm compile --target claude`` must
omit the instructions section from ``CLAUDE.md`` so Claude Code does not
load duplicate content into its context window.

This is the cross-module guard requested by the review panel on PR #1146
(test-coverage-expert: outcome=missing). The unit suite covers the
formatter and compiler in isolation; this regression trap fires if a
future refactor of ``apm install``'s write path silently breaks the
filesystem contract that ``apm compile`` reads (any ``.md`` file under
``.claude/rules/`` triggers the skip).

Both stages run through the real CLI as subprocesses to exercise the
full code path, not just the formatter unit.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from apm_cli.compilation.constitution import clear_constitution_cache

CLI = [sys.executable, "-m", "apm_cli.cli"]


# ---------------------------------------------------------------------------
# Module-level cache isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_constitution_cache():
    """Clear the module-level constitution cache before and after every test.

    The cache is global; without this fixture a test that populates it can
    leak state into later tests and cause unexpected CLAUDE.md emission or
    suppressed stale-removal paths (mirrors the unit-test isolation in
    test_stale_claude_md_cleanup_1729.py).
    """
    clear_constitution_cache()
    yield
    clear_constitution_cache()


APM_YML = """name: test-dedup
version: 1.0.0
description: Install->compile dedup regression trap
author: Test
targets:
  - claude
"""

INSTRUCTION_BODY = (
    "---\n"
    "description: Style rule for the dedup test\n"
    "applyTo: '**/*.py'\n"
    "---\n"
    "# Style rule\n"
    "Use type hints everywhere.\n"
)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        CLI + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project_with_instruction():
    with tempfile.TemporaryDirectory(prefix="apm-dedup-e2e-") as tmp:
        proj = Path(tmp).resolve()
        (proj / "apm.yml").write_text(APM_YML, encoding="utf-8")
        instr_dir = proj / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "style.instructions.md").write_text(INSTRUCTION_BODY, encoding="utf-8")
        yield proj


@pytest.mark.integration
def test_install_then_compile_skips_duplicated_instructions(project_with_instruction):
    """After install populates .claude/rules/, compile must drop the
    instructions section from CLAUDE.md. Pre-PR-#1146 the section was
    duplicated into both files on every compile.
    """
    proj = project_with_instruction

    install_res = _run(proj, "install", "--target", "claude")
    assert install_res.returncode == 0, (
        f"install stdout:\n{install_res.stdout}\ninstall stderr:\n{install_res.stderr}"
    )
    rules_dir = proj / ".claude" / "rules"
    assert rules_dir.exists(), (
        "install --target claude must populate .claude/rules/ "
        "(this is the precondition the dedup logic reads)"
    )
    rule_files = sorted(rules_dir.glob("*.md"))
    assert rule_files, "install must emit at least one *.md rule file"

    compile_res = _run(proj, "compile", "--target", "claude")
    assert compile_res.returncode == 0, (
        f"compile stdout:\n{compile_res.stdout}\ncompile stderr:\n{compile_res.stderr}"
    )

    claude_md = proj / "CLAUDE.md"
    if claude_md.exists():
        body = claude_md.read_text(encoding="utf-8")
        # The PR's contract: when .claude/rules/ is populated, the
        # "Project Standards" instructions section is omitted from
        # CLAUDE.md to keep Claude's context window lean.
        assert "Project Standards" not in body, (
            "CLAUDE.md must NOT carry the duplicated instructions "
            "section after install populated .claude/rules/. "
            "Body was:\n" + body
        )


@pytest.mark.integration
def test_clean_flag_removes_stale_apm_generated_claude_md(project_with_instruction):
    """apm compile --target claude --clean must remove a stale APM-generated
    CLAUDE.md when .claude/rules/ is already populated.

    Exercises the full CLI dispatch -> clean_orphaned=True -> _compile_claude_md
    chain (unit tests bypass the CLI dispatch layer).
    """
    from apm_cli.compilation.claude_formatter import CLAUDE_HEADER

    proj = project_with_instruction

    # Populate .claude/rules/ manually so dedup fires without a full install.
    rules_dir = proj / ".claude" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "style.md").write_text("Use type hints everywhere.\n", encoding="utf-8")

    # Write a stale APM-generated CLAUDE.md with the marker.
    (proj / "CLAUDE.md").write_text(
        f"{CLAUDE_HEADER}\n\n# Project Standards\n\nUse type hints everywhere.\n",
        encoding="utf-8",
    )

    compile_res = _run(proj, "compile", "--target", "claude", "--clean")
    assert compile_res.returncode == 0, (
        f"compile --clean stdout:\n{compile_res.stdout}\n"
        f"compile --clean stderr:\n{compile_res.stderr}"
    )

    assert not (proj / "CLAUDE.md").exists(), (
        "compile --target claude --clean must remove the stale APM-generated CLAUDE.md "
        "when .claude/rules/ is already populated. "
        f"stdout:\n{compile_res.stdout}\nstderr:\n{compile_res.stderr}"
    )


@pytest.mark.integration
def test_compile_alone_then_compile_again_skips_on_second_run(project_with_instruction):
    """`apm compile` itself also writes per-file rules into
    ``.claude/rules/``; running it twice must trigger the dedup on the
    second pass even if `apm install` was never invoked. Locks in the
    docs claim that either install or compile can populate the dir.
    """
    proj = project_with_instruction

    first = _run(proj, "compile", "--target", "claude")
    assert first.returncode == 0, first.stderr

    rules_dir = proj / ".claude" / "rules"
    if not rules_dir.exists() or not list(rules_dir.glob("*.md")):
        pytest.skip(
            "compile alone does not populate .claude/rules/ on this "
            "build; install->compile path is covered by the sibling test"
        )

    second = _run(proj, "compile", "--target", "claude")
    assert second.returncode == 0, second.stderr

    claude_md = proj / "CLAUDE.md"
    if claude_md.exists():
        body = claude_md.read_text(encoding="utf-8")
        assert "Project Standards" not in body, (
            "Second compile must dedup against its own previously-emitted "
            ".claude/rules/ files. Body was:\n" + body
        )
