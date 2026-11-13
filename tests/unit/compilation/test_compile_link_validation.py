"""Compile-time link validation for suffixed markdown targets.

Issue #3255: a target such as ``references/standards.md#template`` names a
file plus a section, not a file called ``standards.md#template``. ``apm
compile`` must validate the filesystem path without the suffix and keep the
suffix out of missing-file diagnostics.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli


def _write_instruction(project: Path) -> None:
    instruction = project / ".apm" / "instructions" / "test.instructions.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text(
        """---
description: suffixed link fixture
applyTo: "**/*.py"
---
See [template](references/standards.md#template),
[query](references/standards.md?view=full),
[combined](references/standards.md?view=full#template), and
[missing](references/missing.md#template).
""",
        encoding="utf-8",
    )


def test_compile_dry_run_validates_suffixed_links_by_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: test-project\nversion: 0.1.0\n", encoding="utf-8")
    _write_instruction(tmp_path)
    references = tmp_path / ".apm" / "instructions" / "references"
    references.mkdir()
    (references / "standards.md").write_text("# Template\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["compile", "--dry-run"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Referenced file not found: references/standards.md" not in output
    assert "Referenced file not found: references/missing.md (in link 'missing')" in output
    assert not (tmp_path / "AGENTS.md").exists()
