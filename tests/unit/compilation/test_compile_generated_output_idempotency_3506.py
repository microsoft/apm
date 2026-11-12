"""Regression tests for #3506: placement ignores APM-generated context outputs.

The placement optimizer must count only authored content. Context files that a
previous ``apm compile`` wrote (and that carry the APM-generated marker header)
must not feed back into ``applyTo`` match counting on the next compile.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.claude_formatter import CLAUDE_HEADER
from apm_cli.compilation.constants import (
    AGENTS_MD_GENERATED_MARKER,
    DISTRIBUTED_AGENTS_MD_GENERATED_MARKER,
    GEMINI_MD_GENERATED_MARKER,
)
from apm_cli.compilation.context_optimizer import ContextOptimizer

pytestmark = pytest.mark.component

_INSTRUCTION = """---
description: Guides tree conventions
applyTo: docs/guides/**
---

## Guides

Keep guides focused.
"""


def _write_issue_fixture(root: Path) -> Path:
    (root / "apm.yml").write_text("name: repro-package\nversion: 0.1.0\n", encoding="utf-8")
    instructions_dir = root / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    instruction = instructions_dir / "0100-guides.instructions.md"
    instruction.write_text(_INSTRUCTION, encoding="utf-8")
    for name in ("a", "b"):
        page_dir = root / "docs" / "guides" / name
        page_dir.mkdir(parents=True)
        (page_dir / "page.md").write_text(f"# {name.upper()}\n", encoding="utf-8")
    return instruction


def _analyzed_files(optimizer: ContextOptimizer, root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in optimizer._get_all_files()}


def _analyzed_count(output: str) -> int:
    match = re.search(r"(\d+) files analyzed", output)
    assert match, output
    return int(match.group(1))


def test_repeated_compile_keeps_subtree_placement_without_orphan_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three compiles of the issue fixture produce the same placement and counts."""
    monkeypatch.chdir(tmp_path)
    _write_issue_fixture(tmp_path)
    runner = CliRunner()
    subtree_agents = tmp_path / "docs" / "guides" / "AGENTS.md"
    root_agents = tmp_path / "AGENTS.md"

    for run in range(1, 4):
        result = runner.invoke(cli, ["compile"])
        assert result.exit_code == 0, f"run {run}: {result.output}"
        assert "3 files analyzed" in result.output, f"run {run}: {result.output}"
        assert "Orphaned" not in result.output, f"run {run}: {result.output}"
        assert "orphaned" not in result.output, f"run {run}: {result.output}"
        assert subtree_agents.is_file(), f"run {run}"
        assert not root_agents.exists(), f"run {run}: placement flipped to root"


def test_genuine_orphan_is_still_reported_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orphan detection and --clean still act on stale generated outputs."""
    monkeypatch.chdir(tmp_path)
    instruction = _write_issue_fixture(tmp_path)
    runner = CliRunner()
    first = runner.invoke(cli, ["compile"])
    assert first.exit_code == 0, first.output
    stale = tmp_path / "docs" / "guides" / "AGENTS.md"
    assert stale.is_file()

    other_dir = tmp_path / "docs" / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "page.md").write_text("# Other\n", encoding="utf-8")
    instruction.write_text(
        _INSTRUCTION.replace("docs/guides/**", "docs/other/**"), encoding="utf-8"
    )

    warned = runner.invoke(cli, ["compile"])
    assert warned.exit_code == 0, warned.output
    assert "Orphaned AGENTS.md found: docs/guides/AGENTS.md" in warned.output
    assert stale.is_file()

    cleaned = runner.invoke(cli, ["compile", "--clean"])
    assert cleaned.exit_code == 0, cleaned.output
    assert not stale.exists()
    assert (other_dir / "AGENTS.md").is_file()


@pytest.mark.parametrize(
    ("filename", "marker"),
    [
        ("AGENTS.md", AGENTS_MD_GENERATED_MARKER),
        ("AGENTS.md", DISTRIBUTED_AGENTS_MD_GENERATED_MARKER),
        ("CLAUDE.md", CLAUDE_HEADER),
        ("GEMINI.md", GEMINI_MD_GENERATED_MARKER),
    ],
)
def test_generated_context_outputs_are_excluded_from_placement_files(
    tmp_path: Path, filename: str, marker: str
) -> None:
    """Context outputs bearing their APM marker header never count as content."""
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "page.md").write_text("# Page\n", encoding="utf-8")
    (guides / filename).write_text(f"{marker}\n# Generated\n", encoding="utf-8")

    files = _analyzed_files(ContextOptimizer(base_dir=str(tmp_path)), tmp_path)

    assert files == {"docs/guides/page.md"}


def test_authored_files_are_still_counted(tmp_path: Path) -> None:
    """Hand-authored context files and non-context marker text remain content."""
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "page.md").write_text("# Page\n", encoding="utf-8")
    (guides / "AGENTS.md").write_text("# Hand-authored guidance\n", encoding="utf-8")
    (guides / "CLAUDE.md").write_text(
        f"# Notes\n\n{CLAUDE_HEADER} appears later in body text only\n", encoding="utf-8"
    )
    (guides / "notes.md").write_text(f"{AGENTS_MD_GENERATED_MARKER}\n# Notes\n", encoding="utf-8")
    # A marker owned by a different context file does not mark this file as generated.
    (guides / "GEMINI.md").write_text(f"{AGENTS_MD_GENERATED_MARKER}\n# Gemini\n", encoding="utf-8")

    files = _analyzed_files(ContextOptimizer(base_dir=str(tmp_path)), tmp_path)

    assert files == {
        "docs/guides/page.md",
        "docs/guides/AGENTS.md",
        "docs/guides/CLAUDE.md",
        "docs/guides/notes.md",
        "docs/guides/GEMINI.md",
    }


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_named_pipe_context_filename_does_not_block_analysis(tmp_path: Path) -> None:
    """A FIFO named like a context file is never opened by the generated probe."""
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "page.md").write_text("# Page\n", encoding="utf-8")
    os.mkfifo(guides / "AGENTS.md")
    result: dict[str, set[str]] = {}

    def analyze() -> None:
        result["files"] = _analyzed_files(ContextOptimizer(base_dir=str(tmp_path)), tmp_path)

    worker = threading.Thread(target=analyze, daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "placement analysis blocked opening a named pipe"
    assert result["files"] == {"docs/guides/page.md", "docs/guides/AGENTS.md"}


@pytest.mark.parametrize("prefix_lines", [1, 12], ids=["short-prefix", "long-prefix"])
def test_managed_section_agents_md_with_authored_prefix_stays_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix_lines: int
) -> None:
    """Mixed-ownership AGENTS.md keeps counting as authored across compiles."""
    monkeypatch.chdir(tmp_path)
    instructions_dir = tmp_path / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "0100-guides.instructions.md").write_text(_INSTRUCTION, encoding="utf-8")
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "page.md").write_text("# Page\n", encoding="utf-8")
    (tmp_path / "apm.yml").write_text(
        "name: repro-package\nversion: 0.1.0\n"
        "compilation:\n  agents_md:\n    mode: managed_section\n",
        encoding="utf-8",
    )
    authored_prefix = "".join(f"Team note {i}.\n" for i in range(prefix_lines))
    mixed = tmp_path / "docs" / "guides" / "AGENTS.md"
    mixed.write_text(f"{authored_prefix}<!-- apm:start -->\n<!-- apm:end -->\n", encoding="utf-8")
    runner = CliRunner()

    counts = []
    for run in range(1, 4):
        result = runner.invoke(cli, ["compile"])
        assert result.exit_code == 0, f"run {run}: {result.output}"
        assert "rphaned" not in result.output, f"run {run}: {result.output}"
        counts.append(_analyzed_count(result.output))
        content = mixed.read_text(encoding="utf-8")
        assert content.startswith(authored_prefix), f"run {run}"
        assert "Keep guides focused." in content, f"run {run}: placement moved"

    assert counts == [counts[0]] * 3, counts


def test_managed_section_wrapper_without_authored_text_is_generated(tmp_path: Path) -> None:
    """An APM-created managed wrapper is output; authored text around it is content."""
    markers = ("<!-- apm:start -->", "<!-- apm:end -->")
    generated_block = f"# AGENTS.md\n{DISTRIBUTED_AGENTS_MD_GENERATED_MARKER}\n\nGuidance.\n"
    wrapper_dir = tmp_path / "docs" / "wrapper"
    mixed_dir = tmp_path / "docs" / "mixed"
    for directory in (wrapper_dir, mixed_dir):
        directory.mkdir(parents=True)
        (directory / "page.md").write_text("# Page\n", encoding="utf-8")
    (wrapper_dir / "AGENTS.md").write_text(
        f"{markers[0]}\n{generated_block}{markers[1]}\n", encoding="utf-8"
    )
    (mixed_dir / "AGENTS.md").write_text(
        f"{markers[0]}\n{generated_block}{markers[1]}\nTeam footer.\n", encoding="utf-8"
    )

    optimizer = ContextOptimizer(base_dir=str(tmp_path), managed_section_markers=markers)

    assert _analyzed_files(optimizer, tmp_path) == {
        "docs/wrapper/page.md",
        "docs/mixed/page.md",
        "docs/mixed/AGENTS.md",
    }
