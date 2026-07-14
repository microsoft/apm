"""Integration: install -> compile deduplication for Antigravity / AGENTS.md.

Pins the user-visible promise across the install and compile boundaries:
after ``apm install --target antigravity`` writes native rules under
``.agents/rules/``, a subsequent ``apm compile --target antigravity`` must
omit duplicate instruction content from ``AGENTS.md``. An unrelated markdown
file in the rules directory must not trigger that deduplication.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

APM_YML = """name: test-antigravity-dedup
version: 1.0.0
description: Install->compile dedup regression trap for Antigravity/AGENTS.md
author: Test
targets:
  - antigravity
"""

INSTRUCTION_BODY = (
    "---\n"
    "description: Style rule for the Antigravity dedup test\n"
    'applyTo: "src/**/*.py"\n'
    "---\n"
    "# Style rule\n"
    "Use type hints everywhere.\n"
)

EXPECTED_RULE = (
    '---\ntrigger: glob\nglobs: "src/**/*.py"\n---\n\n# Style rule\nUse type hints everywhere.\n'
)

INSTRUCTION_SENTINEL = "Use type hints everywhere."


def _run(apm_binary_path: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm_binary_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project_with_instruction(tmp_path: Path) -> Path:
    (tmp_path / "apm.yml").write_text(APM_YML, encoding="utf-8")
    instr_dir = tmp_path / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "style.instructions.md").write_text(
        INSTRUCTION_BODY,
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.integration
def test_install_then_compile_dedups_only_expected_antigravity_rule(
    project_with_instruction: Path,
    apm_binary_path: Path,
) -> None:
    """Real install+compile proves Antigravity rule frontmatter and dedup."""
    proj = project_with_instruction

    install_res = _run(apm_binary_path, proj, "install", "--target", "antigravity")
    assert install_res.returncode == 0, (
        f"install stdout:\n{install_res.stdout}\ninstall stderr:\n{install_res.stderr}"
    )

    rules_dir = proj / ".agents" / "rules"
    rule_file = rules_dir / "style.md"
    assert rule_file.read_text(encoding="utf-8") == EXPECTED_RULE

    dedup_res = _run(apm_binary_path, proj, "compile", "--target", "antigravity")
    assert dedup_res.returncode == 0, (
        f"dedup compile stdout:\n{dedup_res.stdout}\ndedup compile stderr:\n{dedup_res.stderr}"
    )
    agents_md = proj / "AGENTS.md"
    if agents_md.exists():
        assert INSTRUCTION_SENTINEL not in agents_md.read_text(encoding="utf-8")

    rule_file.unlink()
    (rules_dir / "unrelated.md").write_text(
        "# Unrelated\nThis file must not trigger instruction dedup.\n",
        encoding="utf-8",
    )
    if agents_md.exists():
        agents_md.unlink()

    unrelated_res = _run(apm_binary_path, proj, "compile", "--target", "antigravity")
    assert unrelated_res.returncode == 0, (
        f"unrelated compile stdout:\n{unrelated_res.stdout}\n"
        f"unrelated compile stderr:\n{unrelated_res.stderr}"
    )
    assert agents_md.exists(), "AGENTS.md must be generated without a matching rule file"
    assert INSTRUCTION_SENTINEL in agents_md.read_text(encoding="utf-8")
