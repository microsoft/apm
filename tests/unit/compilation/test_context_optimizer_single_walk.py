"""Regression tests for the unified single-walk traversal in ContextOptimizer.

Asserts that ``optimize_instruction_placement`` and ``_get_all_files``
both reuse the same ``os.walk`` traversal result, and that behavioral
correctness is preserved across exclusions, hidden-tool roots, stable
ordering, and cache lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction

pytestmark = pytest.mark.component


def _make_instruction(
    name: str = "inst",
    apply_to: str | None = "**/*.py",
) -> Instruction:
    return Instruction(
        name=name,
        file_path=Path(f"{name}.instructions.md"),
        description=f"{name} description",
        apply_to=apply_to,
        content=f"{name} content",
    )


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


class TestCompileInventoryProjection:
    """Regression coverage for full accounting and scoped candidate projection."""

    def test_single_inventory_populates_directory_cache_and_file_list_cache(
        self, tmp_path: Path
    ) -> None:
        """One inventory walk must populate both optimizer caches."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/utils.py")
        _touch(tmp_path, "tests/test_main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")

        from apm_cli.compilation.inventory import CompileInventory

        with patch(
            "apm_cli.compilation.context_optimizer.CompileInventory.collect",
            wraps=CompileInventory.collect,
        ) as collect:
            optimizer = ContextOptimizer(base_dir=str(tmp_path))
            optimizer.optimize_instruction_placement([instruction])

        assert collect.call_count == 1

        assert optimizer._directory_cache, "_directory_cache must be non-empty after optimization"
        assert optimizer._file_list_cache is not None, "_file_list_cache must not be None"
        assert len(optimizer._file_list_cache) > 0, "_file_list_cache must contain files"

        dir_names = {p.name for p in optimizer._directory_cache}
        assert "src" in dir_names
        assert "tests" in dir_names

        file_names = {p.name for p in optimizer._file_list_cache}
        assert "main.py" in file_names
        assert "utils.py" in file_names
        assert "test_main.py" in file_names

    def test_literal_apply_to_prefix_prunes_unrelated_top_level_subtrees(
        self, tmp_path: Path
    ) -> None:
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "vendor/huge.txt")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="src/**/*.py")])

        assert tmp_path / "src/main.py" in optimizer._file_list_cache
        assert tmp_path / "vendor/huge.txt" not in optimizer._file_list_cache
        assert tmp_path / "vendor" in optimizer._directory_cache

    def test_literal_prefix_matches_full_walk_placement_at_project_scale(
        self, tmp_path: Path
    ) -> None:
        """A 6,602-file tree scopes matching without changing placement output."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/worker.py")
        for directory_index in range(419):
            file_count = 16 if directory_index < 315 else 15
            for file_index in range(file_count):
                _touch(tmp_path, f"pkg-{directory_index:03d}/file-{file_index:02d}.txt")

        instruction = _make_instruction(apply_to="src/**/*.py")
        scoped = ContextOptimizer(base_dir=str(tmp_path))
        scoped_match_calls: list[Path] = []
        original_file_matches = ContextOptimizer._file_matches_pattern

        def counting_file_matches(self: ContextOptimizer, file_path: Path, pattern: str) -> bool:
            scoped_match_calls.append(file_path)
            return original_file_matches(self, file_path, pattern)

        with patch.object(ContextOptimizer, "_file_matches_pattern", counting_file_matches):
            scoped_placement = scoped.optimize_instruction_placement([instruction])

        full = ContextOptimizer(base_dir=str(tmp_path))
        with patch(
            "apm_cli.compilation.context_optimizer.literal_apply_to_top_level_roots",
            return_value=None,
        ):
            full_placement = full.optimize_instruction_placement([instruction])

        def placement_snapshot(
            placement: dict[Path, list[Instruction]],
        ) -> list[tuple[str, tuple[str, ...]]]:
            return sorted(
                (
                    str(directory.relative_to(tmp_path)),
                    tuple(instruction.name for instruction in instructions),
                )
                for directory, instructions in placement.items()
            )

        assert len(full._directory_cache) == 420
        assert len(scoped._directory_cache) == 420
        assert len(full._file_list_cache) == 6602
        assert len(scoped._file_list_cache) == 2
        assert placement_snapshot(scoped_placement) == placement_snapshot(full_placement)
        assert placement_snapshot(scoped_placement) == [("src", ("inst",))]
        assert set(scoped_match_calls) <= set(scoped._file_list_cache)
        assert len(scoped_match_calls) <= len(scoped._file_list_cache)
        assert scoped._optimization_decisions[0].matching_directories == 1
        assert full._optimization_decisions[0].matching_directories == 1

    @pytest.mark.parametrize(
        "apply_to",
        [
            None,
            "**/*.py",
            "*.py",
            "{src,docs}/**",
            "*/src/**/*.py",
            "src/**/*.py,**/*.md",
            "src/{api,cli/**",
        ],
    )
    def test_unprovable_prefix_keeps_full_walk(self, tmp_path: Path, apply_to: str | None) -> None:
        """A global or ambiguous segment kills the root-pruning mutation."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "vendor/huge.txt")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to=apply_to)])

        assert tmp_path / "vendor/huge.txt" in optimizer._file_list_cache
        assert tmp_path / "vendor" in optimizer._directory_cache

    def test_unscoped_candidates_do_not_build_redundant_full_file_projection(
        self, tmp_path: Path
    ) -> None:
        """Global fallback reuses each inventory directory's complete file list."""
        from apm_cli.compilation.inventory import CompileInventory

        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "vendor/huge.txt")
        inventory = CompileInventory.collect(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path), inventory=inventory)

        with patch.object(
            CompileInventory,
            "files_under",
            side_effect=AssertionError("unscoped projection must reuse complete directory files"),
        ):
            optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        assert set(optimizer._file_list_cache) == {
            tmp_path / "src/main.py",
            tmp_path / "vendor/huge.txt",
        }

    def test_comma_list_unions_ten_literal_roots(self, tmp_path: Path) -> None:
        """Comma lists retain every literal root and prune unrelated siblings."""
        roots = [f"package-{index:02d}" for index in range(10)]
        for root in roots:
            _touch(tmp_path, f"{root}/src/main.py")
        _touch(tmp_path, "vendor/huge.txt")
        apply_to = ",".join(f"{root}/src/**/*.py" for root in roots)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to=apply_to)])

        assert set(optimizer._scan_top_level_roots or ()) == set(roots)
        assert tmp_path / "vendor/huge.txt" not in optimizer._file_list_cache
        assert {path.parent.parent.name for path in optimizer._file_list_cache} == set(roots)
        assert optimizer._optimization_decisions[0].matching_directories == len(roots)

    def test_hidden_root_and_literal_root_are_scanned_together(self, tmp_path: Path) -> None:
        """A targeted hidden root remains eligible alongside ordinary roots."""
        _touch(tmp_path, ".github/instructions/guide.md")
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "vendor/huge.txt")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement(
            [_make_instruction(apply_to=".github/**/*.md,src/**/*.py")]
        )

        assert tmp_path / ".github/instructions/guide.md" in optimizer._file_list_cache
        assert tmp_path / "src/main.py" in optimizer._file_list_cache
        assert tmp_path / "vendor/huge.txt" not in optimizer._file_list_cache
        assert optimizer._optimization_decisions[0].matching_directories == 2

    def test_excluded_literal_root_stays_excluded(self, tmp_path: Path) -> None:
        """Configured exclusions still win when applyTo names their root."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "vendor/generated.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=["vendor"])
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="vendor/**/*.py")])

        assert tmp_path / "vendor/generated.py" not in optimizer._file_list_cache
        assert tmp_path / "vendor" not in optimizer._directory_cache
        assert optimizer._optimization_decisions[0].matching_directories == 0

    def test_universal_scan_does_not_follow_directory_symlinks(self, tmp_path: Path) -> None:
        """The root filter leaves the no-follow traversal contract unchanged."""
        _touch(tmp_path, "src/main.py")
        (tmp_path / "linked").symlink_to(tmp_path / "src", target_is_directory=True)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        assert tmp_path / "src/main.py" in optimizer._file_list_cache
        assert tmp_path / "linked" not in optimizer._directory_cache
        assert optimizer._optimization_decisions[0].matching_directories == 1


class TestGetAllFilesRoutesThroughAnalyze:
    """_get_all_files must project the inventory only once."""

    def test_get_all_files_before_optimize_triggers_single_walk(self, tmp_path: Path) -> None:
        """_get_all_files called before optimize must project the inventory."""
        _touch(tmp_path, "app.py")
        _touch(tmp_path, "lib/helper.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        assert optimizer._file_list_cache is None

        files = optimizer._get_all_files()

        assert optimizer._file_list_cache is not None
        assert files is optimizer._file_list_cache
        assert set(optimizer._directory_cache) == {tmp_path, tmp_path / "lib"}
        assert optimizer._directory_cache[tmp_path].total_files == 1
        assert optimizer._directory_cache[tmp_path / "lib"].total_files == 1

    def test_optimize_rebuilds_direct_cache_for_selected_hidden_root(self, tmp_path: Path) -> None:
        """Optimize must replace a direct-call cache when hidden roots become eligible."""
        _touch(tmp_path, ".github/instructions/guide.md")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        initial_files = optimizer._get_all_files()

        assert tmp_path / ".github/instructions/guide.md" not in initial_files
        assert tmp_path / ".github/instructions" not in optimizer._directory_cache

        optimizer.optimize_instruction_placement([_make_instruction(apply_to=".github/**/*.md")])

        assert tmp_path / ".github/instructions/guide.md" in optimizer._file_list_cache
        assert tmp_path / ".github/instructions" in optimizer._directory_cache

    def test_get_all_files_reuses_cache_without_second_walk(self, tmp_path: Path) -> None:
        """After the first projection, a second call reuses the cache."""
        _touch(tmp_path, "app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        first = optimizer._get_all_files()

        second = optimizer._get_all_files()

        assert first is second


class TestExclusionInUnifiedWalk:
    """DEFAULT_EXCLUDED_DIRNAMES and configurable patterns honored in unified walk."""

    def test_default_excluded_dirnames_absent_from_both_caches(self, tmp_path: Path) -> None:
        """Files inside node_modules and __pycache__ must not appear in either cache."""
        _touch(tmp_path, "src/app.py")
        _touch(tmp_path, "node_modules/pkg/index.js")
        _touch(tmp_path, "__pycache__/module.pyc")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        dir_names = {p.name for p in optimizer._directory_cache}
        file_names = {p.name for p in optimizer._file_list_cache}

        assert "node_modules" not in dir_names
        assert "__pycache__" not in dir_names
        assert "index.js" not in file_names
        assert "module.pyc" not in file_names

    def test_configurable_exclude_patterns_prune_both_caches(self, tmp_path: Path) -> None:
        """Paths matching configurable exclude patterns absent from both caches."""
        _touch(tmp_path, "src/app.py")
        _touch(tmp_path, "vendor/lib.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=["vendor"])
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        dir_names = {p.name for p in optimizer._directory_cache}
        file_names = {p.name for p in optimizer._file_list_cache}

        assert "vendor" not in dir_names
        assert "lib.py" not in file_names


class TestHiddenToolRootsInUnifiedWalk:
    """Supported hidden-tool roots in applyTo are admitted; others are pruned."""

    def test_supported_hidden_root_in_apply_to_traversed_by_unified_walk(
        self, tmp_path: Path
    ) -> None:
        """A supported hidden root named in applyTo appears in both caches."""
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        instr_dir = apm_dir / "instructions"
        instr_dir.mkdir()
        (instr_dir / "guide.md").touch()

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to=".apm/**/*.md")
        optimizer.optimize_instruction_placement([instruction])

        dir_names = {p.name for p in optimizer._directory_cache}
        file_names = {p.name for p in optimizer._file_list_cache}

        assert "instructions" in dir_names
        assert "guide.md" in file_names

    def test_unsupported_hidden_dir_pruned_from_both_caches(self, tmp_path: Path) -> None:
        """A hidden directory not in PLACEMENT_HIDDEN_TOOL_TREES is pruned."""
        hidden = tmp_path / ".secret"
        hidden.mkdir()
        (hidden / "data.txt").touch()
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="src/**/*.py")])

        dir_names = {p.name for p in optimizer._directory_cache}
        file_names = {p.name for p in optimizer._file_list_cache}

        assert ".secret" not in dir_names
        assert "data.txt" not in file_names


class TestStableOrderingInUnifiedWalk:
    """File list from the unified walk must be in stable sorted order."""

    def test_file_list_cache_sorted_within_directory(self, tmp_path: Path) -> None:
        """Files within each directory appear in sorted (lexicographic) order."""
        _touch(tmp_path, "src/z_last.py")
        _touch(tmp_path, "src/a_first.py")
        _touch(tmp_path, "src/m_mid.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        src_files = [p.name for p in optimizer._file_list_cache if p.parent.name == "src"]
        assert src_files == sorted(src_files), f"files in src/ not in sorted order: {src_files}"

    def test_file_list_cache_sorted_across_directories(self, tmp_path: Path) -> None:
        """Files from sibling directories follow sorted traversal order."""
        _touch(tmp_path, "zeta/a.py")
        _touch(tmp_path, "alpha/z.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        relative_files = [path.relative_to(tmp_path) for path in optimizer._file_list_cache]
        assert relative_files == [Path("alpha/z.py"), Path("zeta/a.py")]

    def test_file_list_cache_stable_across_repeated_optimize_calls(self, tmp_path: Path) -> None:
        """Repeated optimization calls produce identical file lists."""
        for name in ["z.py", "a.py", "m.py"]:
            _touch(tmp_path, f"src/{name}")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")

        optimizer.optimize_instruction_placement([instruction])
        first = list(optimizer._file_list_cache)

        optimizer.optimize_instruction_placement([instruction])
        second = list(optimizer._file_list_cache)

        assert first == second, "file list must be identical across repeated runs"
