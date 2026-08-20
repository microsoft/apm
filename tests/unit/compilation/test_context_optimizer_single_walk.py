"""Regression tests for the unified single-walk traversal in ContextOptimizer.

Asserts that ``optimize_instruction_placement`` and ``_get_all_files``
both reuse the same ``os.walk`` traversal result, and that behavioral
correctness is preserved across exclusions, hidden-tool roots, stable
ordering, and cache lifecycle.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction

pytestmark = pytest.mark.component


def _make_instruction(
    name: str = "inst",
    apply_to: str = "**/*.py",
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


def _walk_with_nested_subtree(
    base: Path,
    subtree_name: str,
    descended: list[Path],
) -> Iterator[tuple[str, list[str], list[str]]]:
    root_dirs = [subtree_name]
    yield str(base), root_dirs, ["app.py"]
    if subtree_name not in root_dirs:
        return

    subtree = base / subtree_name
    child_dirs = ["deep"]
    yield str(subtree), child_dirs, []
    if child_dirs:
        deep = subtree / "deep"
        descended.append(deep)
        yield str(deep), [], ["ignored.py"]


class TestSingleWalkPopulatesBothCaches:
    """Regression: optimize_instruction_placement must call os.walk exactly once
    and populate both _directory_cache and _file_list_cache in that single pass.
    """

    def test_single_walk_populates_directory_cache_and_file_list_cache(
        self, tmp_path: Path
    ) -> None:
        """One os.walk traversal must populate both caches.

        Regression for the dual-walk footprint: before the fix,
        _analyze_project_structure and _get_all_files each ran their own
        os.walk, doubling filesystem I/O on every compile.
        """
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/utils.py")
        _touch(tmp_path, "tests/test_main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")

        real_walk = os.walk
        walk_call_count = 0

        def counting_walk(top, **kwargs):
            nonlocal walk_call_count
            walk_call_count += 1
            yield from real_walk(top, **kwargs)

        with patch("apm_cli.compilation.context_optimizer.os.walk", side_effect=counting_walk):
            optimizer.optimize_instruction_placement([instruction])

        assert walk_call_count == 1, (
            f"expected exactly 1 os.walk call, got {walk_call_count} "
            "(dual-walk regression: _analyze_project_structure and _get_all_files "
            "must share the same traversal result)"
        )

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


class TestGetAllFilesRoutesThroughAnalyze:
    """_get_all_files must route through _analyze_project_structure when cache is None."""

    def test_get_all_files_before_optimize_triggers_single_walk(self, tmp_path: Path) -> None:
        """_get_all_files called before optimize must use _analyze_project_structure."""
        _touch(tmp_path, "app.py")
        _touch(tmp_path, "lib/helper.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        assert optimizer._file_list_cache is None

        real_walk = os.walk
        walk_call_count = 0

        def counting_walk(top, **kwargs):
            nonlocal walk_call_count
            walk_call_count += 1
            yield from real_walk(top, **kwargs)

        with patch("apm_cli.compilation.context_optimizer.os.walk", side_effect=counting_walk):
            files = optimizer._get_all_files()

        assert walk_call_count == 1
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
        """After the first walk, a second call to _get_all_files reuses the cache."""
        _touch(tmp_path, "app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        first = optimizer._get_all_files()

        real_walk = os.walk
        walk_call_count = 0

        def counting_walk(top, **kwargs):
            nonlocal walk_call_count
            walk_call_count += 1
            yield from real_walk(top, **kwargs)

        with patch("apm_cli.compilation.context_optimizer.os.walk", side_effect=counting_walk):
            second = optimizer._get_all_files()

        assert walk_call_count == 0, "second _get_all_files call must not walk again"
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

    def test_default_exclusion_safety_net_prunes_nested_subtree(self, tmp_path: Path) -> None:
        """The safety net stops descent when parent pruning is bypassed."""
        _touch(tmp_path, "app.py")
        descended: list[Path] = []
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        with (
            patch(
                "apm_cli.compilation.context_optimizer.os.walk",
                side_effect=lambda _top: _walk_with_nested_subtree(
                    tmp_path,
                    "node_modules",
                    descended,
                ),
            ),
            patch.object(optimizer, "_should_exclude_subdir", return_value=False),
        ):
            optimizer._analyze_project_structure()

        assert descended == []

    def test_configurable_exclusion_safety_net_prunes_nested_subtree(
        self,
        tmp_path: Path,
    ) -> None:
        """A current-path exclusion stops descent when parent pruning is bypassed."""
        _touch(tmp_path, "app.py")
        descended: list[Path] = []
        optimizer = ContextOptimizer(base_dir=str(tmp_path), exclude_patterns=["vendor"])

        with (
            patch(
                "apm_cli.compilation.context_optimizer.os.walk",
                side_effect=lambda _top: _walk_with_nested_subtree(
                    tmp_path,
                    "vendor",
                    descended,
                ),
            ),
            patch.object(optimizer, "_should_exclude_subdir", return_value=False),
        ):
            optimizer._analyze_project_structure()

        assert descended == []


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
