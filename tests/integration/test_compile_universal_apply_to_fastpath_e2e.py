"""E2E coverage for universal applyTo fast-path placement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.compilation.inventory import CompileInventory

UNIVERSAL_SENTINEL = "Universal fast path sentinel."
EXPLICIT_SENTINEL = "Explicit all files sentinel."


def _write_project_file(project_root: Path, relative_path: str, content: str = "x\n") -> None:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _relative_directory_set(project_root: Path, directories: set[Path]) -> set[str]:
    return {path.relative_to(project_root).as_posix() or "." for path in directories}


def _build_project(project_root: Path) -> set[str]:
    (project_root / "apm.yml").write_text(
        "name: universal-fastpath-e2e\nversion: 0.1.0\ntarget: agents\n",
        encoding="utf-8",
    )
    instructions_dir = project_root / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "universal.instructions.md").write_text(
        "---\n"
        "description: Universal placement rule\n"
        'applyTo: "  **  "\n'
        "---\n\n"
        f"{UNIVERSAL_SENTINEL}\n",
        encoding="utf-8",
    )
    (instructions_dir / "explicit-all.instructions.md").write_text(
        "---\n"
        "description: Explicit all-files placement rule\n"
        'applyTo: "**/*"\n'
        "---\n\n"
        f"{EXPLICIT_SENTINEL}\n",
        encoding="utf-8",
    )

    expected_directories = {
        ".",
        "docs",
        "src",
        "src/pkg",
        "tests",
    }
    _write_project_file(project_root, "docs/readme.md")
    _write_project_file(project_root, "src/app.py")
    _write_project_file(project_root, "src/pkg/mod.py")
    _write_project_file(project_root, "tests/test_app.py")
    (project_root / "empty").mkdir()
    return expected_directories


def _agents_snapshot(project_root: Path) -> dict[str, bytes]:
    """Return generated AGENTS.md files keyed by portable project path."""
    return {
        path.relative_to(project_root).as_posix(): path.read_bytes()
        for path in sorted(project_root.rglob("AGENTS.md"))
    }


def _write_literal_scope_fixture(project_root: Path, apply_to: str) -> None:
    """Create a placement-threshold fixture with unrelated project accounting."""
    (project_root / "apm.yml").write_text(
        "name: literal-scope-e2e\nversion: 0.1.0\ntarget: agents\n",
        encoding="utf-8",
    )
    instructions_dir = project_root / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "literal.instructions.md").write_text(
        "---\n"
        "description: Literal-root placement rule\n"
        f'applyTo: "{apply_to}"\n'
        "---\n\n"
        "Literal scope sentinel.\n",
        encoding="utf-8",
    )
    for directory in ("api", "cli", "worker", "web"):
        _write_project_file(project_root, f"src/{directory}/app.py")
    # Keep the historical ``src`` placement candidate populated without
    # widening the instruction's four matching directories.
    _write_project_file(project_root, "src/README.md")
    for index in range(20):
        _write_project_file(project_root, f"vendor-{index:02d}/large.txt")


def test_compile_agents_universal_apply_to_fast_path_preserves_match_set(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Real compile must use the ``**`` fast path without changing matches."""
    project_root = tmp_path
    expected_directories = _build_project(project_root)
    optimizer_instances: list[ContextOptimizer] = []
    universal_match_sets: list[set[str]] = []
    explicit_match_sets: list[set[str]] = []
    universal_file_match_calls: list[str] = []

    original_init = ContextOptimizer.__init__
    original_find = ContextOptimizer._find_matching_directories
    original_file_matches = ContextOptimizer._file_matches_pattern

    def spy_init(self: ContextOptimizer, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        optimizer_instances.append(self)

    def spy_find(self: ContextOptimizer, pattern: str) -> set[Path]:
        result = original_find(self, pattern)
        relative_result = _relative_directory_set(project_root, result)
        if pattern.strip() == "**":
            universal_match_sets.append(relative_result)
        elif pattern == "**/*":
            explicit_match_sets.append(relative_result)
        return result

    def spy_file_matches(self: ContextOptimizer, file_path: Path, pattern: str) -> bool:
        if pattern.strip() == "**":
            universal_file_match_calls.append(file_path.name)
        return original_file_matches(self, file_path, pattern)

    monkeypatch.chdir(project_root)
    with (
        patch.object(ContextOptimizer, "__init__", spy_init),
        patch.object(ContextOptimizer, "_find_matching_directories", spy_find),
        patch.object(ContextOptimizer, "_file_matches_pattern", spy_file_matches),
    ):
        result = CliRunner().invoke(
            cli,
            ["compile", "--target", "agents"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    assert optimizer_instances, "compile must construct the real context optimizer"
    assert universal_match_sets == [expected_directories]
    assert explicit_match_sets == [expected_directories]
    assert universal_match_sets[0] == explicit_match_sets[0]
    assert not universal_file_match_calls, (
        "padded applyTo '**' must not fall back to per-file matching during "
        "compile display or inheritance analysis"
    )

    optimizer = optimizer_instances[-1]
    assert _relative_directory_set(project_root, optimizer._pattern_cache["**"]) == (
        expected_directories
    )
    assert "  **  " not in optimizer._pattern_cache
    assert optimizer._optimization_decisions[0].matching_directories == len(expected_directories)
    assert optimizer._optimization_decisions[1].matching_directories == len(expected_directories)

    for directory, analysis in optimizer._directory_cache.items():
        relative_dir = directory.relative_to(project_root).as_posix() or "."
        assert relative_dir in expected_directories
        assert analysis.pattern_matches["**"] == analysis.total_files
        assert "  **  " not in analysis.pattern_matches

    agents_md = project_root / "AGENTS.md"
    assert agents_md.exists()
    agents_content = agents_md.read_text(encoding="utf-8")
    assert UNIVERSAL_SENTINEL in agents_content
    assert EXPLICIT_SENTINEL in agents_content


def test_compile_literal_roots_preserve_generated_artifacts_against_full_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Literal-root pruning must be invisible in generated AGENTS.md artifacts."""
    project_root = tmp_path
    literal_apply_to = "src/**/*.py"
    _write_literal_scope_fixture(project_root, literal_apply_to)

    monkeypatch.chdir(project_root)
    with patch(
        "apm_cli.compilation.inventory.CompileInventory.collect",
        wraps=CompileInventory.collect,
    ) as collect:
        literal_result = CliRunner().invoke(
            cli,
            ["compile", "--target", "agents"],
            catch_exceptions=False,
        )
        assert literal_result.exit_code == 0, literal_result.output
        literal_artifacts = _agents_snapshot(project_root)

        for path in project_root.rglob("AGENTS.md"):
            path.unlink()

        # A separate ``**/*.never`` primitive would become a root-level rule and
        # legitimately alter generated bytes. Force the classifier's documented
        # conservative result instead, while running the real CLI and optimizer.
        with patch(
            "apm_cli.compilation.context_optimizer.literal_apply_to_top_level_roots",
            return_value=None,
        ):
            full_fallback_result = CliRunner().invoke(
                cli,
                ["compile", "--target", "agents"],
                catch_exceptions=False,
            )
        assert full_fallback_result.exit_code == 0, full_fallback_result.output
        assert collect.call_count == 2

    assert _agents_snapshot(project_root) == literal_artifacts
    assert set(literal_artifacts) == {"src/AGENTS.md"}
