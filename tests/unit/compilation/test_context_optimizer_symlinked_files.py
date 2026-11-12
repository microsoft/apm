# Symlinked files match applyTo by their resolved target, not by where the link sits.

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def _symlink(base: Path, rel: str, target: Path) -> Path:
    link = base / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not supported on this platform")
    return link


class TestSymlinkedFileMatchesByPhysicalTarget:
    def test_symlink_to_outside_target_does_not_match(self, tmp_path: Path) -> None:
        outside_target = tmp_path.parent / f"{tmp_path.name}-external-source.ts"
        outside_target.touch()
        link = _symlink(tmp_path, "src/components/Button.ts", outside_target)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        assert optimizer._file_matches_pattern(link, "src/**/*.ts") is False

    @pytest.mark.parametrize("pattern", ["**/*.ts", "**/Button.ts", "src/**/*.ts"])
    def test_outside_symlink_never_matches_recursive_pattern(
        self, tmp_path: Path, pattern: str
    ) -> None:
        outside_target = tmp_path.parent / f"{tmp_path.name}-external-source.ts"
        outside_target.touch()
        link = _symlink(tmp_path, "src/components/Button.ts", outside_target)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        # Second call hits the memoized path.
        results = {optimizer._file_matches_pattern(link, pattern) for _ in range(2)}

        assert results == {False}

    def test_outside_symlink_still_matches_non_recursive_pattern_by_resolved_path(
        self, tmp_path: Path
    ) -> None:
        outside_target = tmp_path.parent / f"{tmp_path.name}-external-source.ts"
        outside_target.touch()
        link = _symlink(tmp_path, "src/components/Button.ts", outside_target)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        assert optimizer._file_matches_pattern(link, "*.ts") is True
        assert optimizer._file_matches_pattern(link, "src/*.ts") is False

    def test_symlink_matches_by_where_its_target_lives_not_where_it_appears(
        self, tmp_path: Path
    ) -> None:
        _touch(tmp_path, "vendor/real/Button.ts")
        link = _symlink(
            tmp_path, "src/components/Button.ts", tmp_path / "vendor" / "real" / "Button.ts"
        )

        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        assert optimizer._file_matches_pattern(link, "src/**/*.ts") is False
        assert optimizer._file_matches_pattern(link, "vendor/**/*.ts") is True

    def test_placement_falls_back_when_symlinked_target_is_outside_project(
        self, tmp_path: Path
    ) -> None:
        outside_target = tmp_path.parent / f"{tmp_path.name}-external-source.ts"
        outside_target.touch()
        _symlink(tmp_path, "src/components/Button.ts", outside_target)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = Instruction(
            name="ts-standards",
            file_path=Path("ts.instructions.md"),
            description="TypeScript standards",
            apply_to="src/**/*.ts",
            content="TS standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        assert optimizer._pattern_cache["src/**/*.ts"] == set()
        assert sum(len(instrs) for instrs in result.values()) == 1

    def test_symlink_with_missing_target_is_excluded(self, tmp_path: Path) -> None:
        # is_file() follows the link to check the target exists.
        dangling_target = tmp_path.parent / f"{tmp_path.name}-does-not-exist.ts"
        link = _symlink(tmp_path, "src/components/Ghost.ts", dangling_target)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = Instruction(
            name="ts-standards",
            file_path=Path("ts.instructions.md"),
            description="TypeScript standards",
            apply_to="src/**/*.ts",
            content="TS standards",
        )

        result = optimizer.optimize_instruction_placement([instruction])

        assert link not in optimizer._files_by_directory.get(link.parent, [])
        placed_dirs = {directory for directory, instrs in result.items() if instrs}
        assert tmp_path / "src" / "components" not in placed_dirs

    def test_symlink_target_collision_with_real_file_does_not_lose_either_directory(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "vendor" / "real" / "Button.ts"
        real.parent.mkdir(parents=True)
        real.touch()
        _symlink(tmp_path, "src/components/Button.ts", real)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("vendor/**/*.ts")

        assert matching == {
            tmp_path / "vendor" / "real",
            tmp_path / "src" / "components",
        }


class TestSymlinkLoopResolveError:
    # Python < 3.13 raises RuntimeError from Path.resolve() on a symlink loop.
    @pytest.fixture
    def loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _touch(tmp_path, "src/loop.md")
        original_resolve = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path.name == "loop.md":
                raise RuntimeError(f"Symlink loop from {path!r}")
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        return tmp_path / "src" / "loop.md"

    def test_recursive_pattern_does_not_match(self, tmp_path: Path, loop: Path) -> None:
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        assert optimizer._file_matches_pattern(loop, "**/*.md") is False

    def test_non_recursive_pattern_matches_unresolved_path(
        self, tmp_path: Path, loop: Path
    ) -> None:
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        assert optimizer._file_matches_pattern(loop, "*.md") is True
        assert optimizer._file_matches_pattern(loop, "src/*.md") is False

    def test_recursive_glob_lists_unresolved_path(self, tmp_path: Path, loop: Path) -> None:
        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._analyze_project_structure()

        assert optimizer._safe_recursive_glob("**/*.md") == [loop.as_posix()]
