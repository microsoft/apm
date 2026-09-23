"""Native coverage uses installed output, without dropping uncovered instructions."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.compilation.instruction_dedup import (
    detect_deployed_instructions,
    uncovered_instructions,
)
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.primitives.models import Instruction

pytestmark = pytest.mark.component


def _instruction(root: Path, name: str = "style", content: str = "Use type hints.") -> Instruction:
    """Create a real source instruction in the native package layout."""
    source = root / ".apm" / "instructions" / f"{name}.instructions.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"---\ndescription: Example\n---\n{content}\n", encoding="utf-8")
    return Instruction(name, source, "Example", "", content)


def _install(root: Path, home: Path) -> Path:
    """Deploy through the real instruction integrator, including link rewriting."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    package_info = SimpleNamespace(install_path=root, deployment_package_root=None)
    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["claude"], package_info, home
    )
    assert result.files_integrated > 0
    return home / ".claude" / "rules"


def test_coverage_matches_install_rendered_links_and_preserves_files(tmp_path: Path) -> None:
    """Compilation recognizes the actual deployed output, not raw source text."""
    root = tmp_path / "apm_modules" / "example"
    instruction = _instruction(root, content="Read [guide](../../guide.md).")
    (root / "guide.md").write_text("Package guide", encoding="utf-8")
    rules_dir = _install(root, tmp_path)
    rule = rules_dir / "style.md"
    before = rule.read_bytes()
    assert before.decode().strip() != instruction.content

    assert uncovered_instructions("claude", [instruction], rules_dir, tmp_path, pytest.fail) == []
    assert rule.read_bytes() == before


def test_partial_coverage_preserves_only_uncovered_instructions(tmp_path: Path) -> None:
    """One deployed rule does not suppress missing or unrelated instructions."""
    root = tmp_path / "package"
    installed = _instruction(root)
    rules_dir = _install(root, tmp_path)
    missing = _instruction(root, "missing", "Keep this instruction.")
    (rules_dir / "unrelated.md").write_text("Unrelated", encoding="utf-8")

    assert uncovered_instructions(
        "claude", [installed, missing], rules_dir, tmp_path, pytest.fail
    ) == [missing]


@pytest.mark.parametrize(
    "content",
    ["Different rule", "---\npaths:\n  - '**/*.py'\n---\nUse type hints."],
)
def test_different_or_scoped_native_rule_keeps_fallback(tmp_path: Path, content: str) -> None:
    """Matching filenames do not prove equivalent unconditional instructions."""
    instruction = _instruction(tmp_path / "package")
    rules_dir = _install(tmp_path / "package", tmp_path)
    (rules_dir / "style.md").write_text(content, encoding="utf-8")

    assert uncovered_instructions("claude", [instruction], rules_dir, tmp_path, pytest.fail) == [
        instruction
    ]


def test_same_filename_omits_only_matching_package_content(tmp_path: Path) -> None:
    """A collision cannot claim that both different instruction bodies are native."""
    first = _instruction(tmp_path / "first", content="First rule")
    second = _instruction(tmp_path / "second", content="Second rule")
    rules_dir = _install(tmp_path / "second", tmp_path)

    assert uncovered_instructions("claude", [first, second], rules_dir, tmp_path, pytest.fail) == [
        first
    ]


@pytest.mark.parametrize("rule_present", [False, True])
def test_absent_rules_do_not_require_reading_source(tmp_path: Path, rule_present: bool) -> None:
    """Instructions discovered without an available source retain their fallback."""
    source = tmp_path / "package" / ".apm" / "instructions" / "missing.instructions.md"
    instruction = Instruction("missing", source, "Example", "", "Fallback")
    rules_dir = tmp_path / "rules"
    if rule_present:
        rules_dir.mkdir()
        (rules_dir / "other.md").write_text("Unrelated", encoding="utf-8")

    assert uncovered_instructions("claude", [instruction], rules_dir, tmp_path, pytest.fail) == [
        instruction
    ]


def test_unreadable_native_rule_keeps_fallback(tmp_path: Path) -> None:
    """Invalid UTF-8 does not turn a native filename into coverage."""
    instruction = _instruction(tmp_path / "package")
    rules_dir = _install(tmp_path / "package", tmp_path)
    (rules_dir / "style.md").write_bytes(b"\xff")
    warnings: list[str] = []

    assert uncovered_instructions(
        "claude", [instruction], rules_dir, tmp_path, warnings.append
    ) == [instruction]
    assert len(warnings) == 1


@pytest.mark.parametrize("symlink_directory", [False, True])
def test_escaping_native_symlink_keeps_fallback(tmp_path: Path, symlink_directory: bool) -> None:
    """Neither a rules directory nor an individual rule may escape the deploy root."""
    home = tmp_path / "home"
    instruction = _instruction(home / "package")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "style.md").write_text(instruction.content, encoding="utf-8")
    rules_dir = home / "rules"
    if symlink_directory:
        rules_dir.symlink_to(outside, target_is_directory=True)
    else:
        rules_dir.mkdir()
        (rules_dir / "style.md").symlink_to(outside / "style.md")
    warnings: list[str] = []

    assert uncovered_instructions("claude", [instruction], rules_dir, home, warnings.append) == [
        instruction
    ]
    assert len(warnings) == 1


def test_project_detection_preserves_any_expected_match(tmp_path: Path) -> None:
    """The shared extraction leaves project-mode matching semantics intact."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "present.md").write_text("Native rule", encoding="utf-8")

    assert detect_deployed_instructions(
        rules_dir, tmp_path, pytest.fail, {"present.md", "missing.md"}
    )
    assert not detect_deployed_instructions(rules_dir, tmp_path, pytest.fail, {"unrelated.md"})


def test_cyclic_native_symlink_keeps_fallback(tmp_path: Path) -> None:
    """Path.resolve may raise RuntimeError for symlink loops on Python 3.11."""
    instruction = _instruction(tmp_path / "package")
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "style.md").symlink_to("style.md")
    warnings: list[str] = []

    assert uncovered_instructions(
        "claude", [instruction], rules_dir, tmp_path, warnings.append
    ) == [instruction]
    assert len(warnings) == 1
