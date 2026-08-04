"""Regression tests for in-memory directory indexes in ContextOptimizer.

Asserts that ``_files_by_directory`` and ``_children_by_directory`` indexes
eliminate all ``Path.iterdir()`` calls from ``_find_matching_directories``
and ``_calculate_inheritance_pollution`` without changing matching-directory
sets, pollution scores, stable ordering, or cache lifecycle.
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


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("symlink creation not supported on this platform")


class TestIndexesPopulatedByCanonicalWalk:
    """_files_by_directory and _children_by_directory built by _analyze_project_structure."""

    def test_files_by_directory_populated(self, tmp_path: Path) -> None:
        """Each admitted directory has its non-hidden files in the index."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/utils.py")
        _touch(tmp_path, "src/.hidden")
        _touch(tmp_path, "tests/test_main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        src_dir = tmp_path / "src"
        assert src_dir in optimizer._files_by_directory
        src_files = {p.name for p in optimizer._files_by_directory[src_dir]}
        assert src_files == {"main.py", "utils.py"}
        assert ".hidden" not in src_files

    def test_children_by_directory_populated(self, tmp_path: Path) -> None:
        """Each admitted directory has its admitted child directories in the index."""
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "src/utils/helpers.py")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        src_dir = tmp_path / "src"
        assert src_dir in optimizer._children_by_directory
        child_names = {p.name for p in optimizer._children_by_directory[src_dir]}
        assert child_names == {"core", "utils"}

    def test_excluded_dirs_absent_from_children_index(self, tmp_path: Path) -> None:
        """Excluded directories (node_modules, __pycache__) not in children index."""
        _touch(tmp_path, "src/app.py")
        _touch(tmp_path, "src/node_modules/pkg/index.js")
        _touch(tmp_path, "src/__pycache__/mod.pyc")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        src_dir = tmp_path / "src"
        child_names = {p.name for p in optimizer._children_by_directory.get(src_dir, [])}
        assert "node_modules" not in child_names
        assert "__pycache__" not in child_names

    def test_children_index_sorted_deterministically(self, tmp_path: Path) -> None:
        """Children are in sorted order matching the walk's subdir sort."""
        _touch(tmp_path, "z_dir/file.py")
        _touch(tmp_path, "a_dir/file.py")
        _touch(tmp_path, "m_dir/file.py")
        _touch(tmp_path, "root.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        children = optimizer._children_by_directory.get(tmp_path, [])
        child_names = [p.name for p in children]
        assert child_names == sorted(child_names)

    def test_empty_directory_absent_from_file_index_but_retained_as_child(
        self,
        tmp_path: Path,
    ) -> None:
        """Empty directories stay in the child index but not the file index."""
        empty_dir = tmp_path / "src/empty"
        empty_dir.mkdir(parents=True)
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        assert empty_dir in optimizer._children_by_directory[tmp_path / "src"]
        assert empty_dir not in optimizer._files_by_directory
        assert empty_dir not in optimizer._directory_cache


class TestNoIterdirCalls:
    """Optimized paths must not call Path.iterdir()."""

    def test_find_matching_directories_no_iterdir(self, tmp_path: Path) -> None:
        """_find_matching_directories does not call Path.iterdir."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/utils.py")
        _touch(tmp_path, "tests/test_main.py")
        _touch(tmp_path, "docs/guide.md")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        # Trigger analysis so indexes are built
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        iterdir_called = False
        original_iterdir = Path.iterdir

        def tracking_iterdir(self_path):
            nonlocal iterdir_called
            iterdir_called = True
            return original_iterdir(self_path)

        with patch.object(Path, "iterdir", tracking_iterdir):
            optimizer._find_matching_directories("**/*.py")

        assert not iterdir_called, (
            "_find_matching_directories must not call Path.iterdir "
            "(should use _files_by_directory index)"
        )

    def test_calculate_inheritance_pollution_no_iterdir(self, tmp_path: Path) -> None:
        """_calculate_inheritance_pollution does not call Path.iterdir."""
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "src/utils/helpers.py")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        iterdir_called = False
        original_iterdir = Path.iterdir

        def tracking_iterdir(self_path):
            nonlocal iterdir_called
            iterdir_called = True
            return original_iterdir(self_path)

        src_dir = tmp_path / "src"
        with patch.object(Path, "iterdir", tracking_iterdir):
            optimizer._calculate_inheritance_pollution(src_dir, "**/*.py")

        assert not iterdir_called, (
            "_calculate_inheritance_pollution must not call Path.iterdir "
            "(should use _children_by_directory index)"
        )


class TestMatchingDirectorySetsUnchanged:
    """Matching-directory sets must be identical to the pre-optimization behavior."""

    def test_matching_dirs_for_recursive_pattern(self, tmp_path: Path) -> None:
        """Recursive glob pattern finds all directories with matching files."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "tests/test_main.py")
        _touch(tmp_path, "docs/guide.md")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("**/*.py")
        expected = {tmp_path / "src", tmp_path / "src/core", tmp_path / "tests"}
        assert matching == expected

    def test_matching_dirs_for_directory_scoped_pattern(self, tmp_path: Path) -> None:
        """Directory-scoped pattern finds only the targeted directory."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "tests/test_main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("src/**/*.py")
        expected = {tmp_path / "src", tmp_path / "src/core"}
        assert matching == expected

    def test_matching_dirs_for_extension_only_pattern(self, tmp_path: Path) -> None:
        """Extension-only pattern matches in all directories with that extension."""
        _touch(tmp_path, "root.md")
        _touch(tmp_path, "docs/guide.md")
        _touch(tmp_path, "src/notes.md")
        _touch(tmp_path, "src/main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("*.md")
        expected = {tmp_path, tmp_path / "docs", tmp_path / "src"}
        assert matching == expected

    def test_matching_dirs_empty_for_no_match(self, tmp_path: Path) -> None:
        """Pattern with no matching files returns empty set."""
        _touch(tmp_path, "src/main.py")
        _touch(tmp_path, "tests/test_main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        matching = optimizer._find_matching_directories("**/*.rs")
        assert matching == set()

    def test_valid_file_symlink_preserves_matching_behavior(self, tmp_path: Path) -> None:
        """A symlink to a regular file remains eligible for pattern matching."""
        _touch(tmp_path, "src/target.py")
        link = tmp_path / "src/alias.py"
        _symlink_or_skip(link, tmp_path / "src/target.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        assert optimizer._find_matching_directories("*.py") == {tmp_path / "src"}
        assert set(optimizer._files_by_directory[tmp_path / "src"]) == {
            link,
            tmp_path / "src/target.py",
        }

    def test_broken_file_symlink_does_not_create_match(self, tmp_path: Path) -> None:
        """A broken file symlink stays in the project cache but cannot match."""
        _touch(tmp_path, "src/readme.txt")
        link = tmp_path / "src/broken.py"
        _symlink_or_skip(link, tmp_path / "missing.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        assert link in optimizer._file_list_cache
        assert link not in optimizer._files_by_directory[tmp_path / "src"]
        assert optimizer._find_matching_directories("*.py") == set()

    def test_symlinked_directory_does_not_match_or_inflate_pollution(self, tmp_path: Path) -> None:
        """A directory symlink is indexed as a child but never analyzed twice."""
        _touch(tmp_path, "src/real/module.py")
        link = tmp_path / "src/linked"
        _symlink_or_skip(link, tmp_path / "src/real", target_is_directory=True)

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        assert link in optimizer._children_by_directory[tmp_path / "src"]
        assert link not in optimizer._directory_cache
        assert optimizer._find_matching_directories("**/*.py") == {tmp_path / "src/real"}
        assert optimizer._calculate_inheritance_pollution(
            tmp_path / "src", "**/*.py"
        ) == pytest.approx(0.0)


class TestPollutionScoresUnchanged:
    """Pollution scores must be identical to the pre-optimization behavior."""

    def test_pollution_score_with_matching_children(self, tmp_path: Path) -> None:
        """No pollution when all children match the pattern."""
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "src/utils/helpers.py")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        src_dir = tmp_path / "src"
        score = optimizer._calculate_inheritance_pollution(src_dir, "**/*.py")
        # All children have .py files, so no pollution
        assert score == 0.0

    def test_pollution_score_with_non_matching_children(self, tmp_path: Path) -> None:
        """Pollution when children have no matching files."""
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "src/assets/logo.png")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        src_dir = tmp_path / "src"
        score = optimizer._calculate_inheritance_pollution(src_dir, "**/*.py")
        # assets/ has no .py files -> pollution penalty 0.5
        assert score == 0.5

    def test_pollution_score_multiple_non_matching_children(self, tmp_path: Path) -> None:
        """Multiple non-matching children accumulate pollution."""
        _touch(tmp_path, "src/core/engine.py")
        _touch(tmp_path, "src/assets/logo.png")
        _touch(tmp_path, "src/static/style.css")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        src_dir = tmp_path / "src"
        score = optimizer._calculate_inheritance_pollution(src_dir, "**/*.py")
        # assets/ and static/ have no .py files -> 0.5 + 0.5 = 1.0
        assert score == 1.0

    def test_pollution_score_leaf_directory(self, tmp_path: Path) -> None:
        """Leaf directory (no children) has zero pollution."""
        _touch(tmp_path, "src/core/engine.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        core_dir = tmp_path / "src/core"
        score = optimizer._calculate_inheritance_pollution(core_dir, "**/*.py")
        assert score == 0.0


class TestCacheResetAndRebuild:
    """Indexes are cleared and rebuilt deterministically on project analysis reset."""

    def test_indexes_cleared_on_optimize_call(self, tmp_path: Path) -> None:
        """optimize_instruction_placement clears and rebuilds indexes."""
        _touch(tmp_path, "src/main.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")

        optimizer.optimize_instruction_placement([instruction])
        first_files_index = dict(optimizer._files_by_directory)
        first_children_index = dict(optimizer._children_by_directory)

        # Second call must clear and rebuild
        optimizer.optimize_instruction_placement([instruction])

        assert optimizer._files_by_directory == first_files_index
        assert optimizer._children_by_directory == first_children_index

    def test_indexes_rebuilt_when_hidden_root_changes(self, tmp_path: Path) -> None:
        """Indexes reflect newly admitted hidden-tool roots after re-optimization."""
        _touch(tmp_path, ".github/instructions/guide.md")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        # First call without targeting .github
        optimizer.optimize_instruction_placement([_make_instruction(apply_to="**/*.py")])

        github_instr_dir = tmp_path / ".github/instructions"
        assert github_instr_dir not in optimizer._files_by_directory

        # Second call targets .github
        optimizer.optimize_instruction_placement([_make_instruction(apply_to=".github/**/*.md")])
        assert github_instr_dir in optimizer._files_by_directory
        file_names = {p.name for p in optimizer._files_by_directory[github_instr_dir]}
        assert "guide.md" in file_names

    def test_get_all_files_populates_indexes(self, tmp_path: Path) -> None:
        """_get_all_files triggers analysis which populates indexes."""
        _touch(tmp_path, "app.py")
        _touch(tmp_path, "lib/helper.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        assert optimizer._files_by_directory == {}
        assert optimizer._children_by_directory == {}

        optimizer._get_all_files()

        assert tmp_path in optimizer._files_by_directory
        assert tmp_path in optimizer._children_by_directory


class TestIterationCountGuard:
    """Deterministic scaling guard: index-based paths must not perform filesystem calls
    proportional to (P * D) where P = patterns and D = directories.
    """

    def test_no_iterdir_scaling_with_patterns(self, tmp_path: Path) -> None:
        """With N patterns and D directories, zero iterdir calls occur.

        Pre-optimization: N*D iterdir calls in _find_matching_directories.
        Post-optimization: 0 iterdir calls (all from in-memory index).
        """
        # Create a representative tree: 20 directories, 3 files each
        for i in range(20):
            for j in range(3):
                _touch(tmp_path, f"dir_{i:02d}/file_{j}.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        optimizer._placement_hidden_tool_trees = frozenset()
        optimizer._analyze_project_structure()

        iterdir_count = 0
        original_iterdir = Path.iterdir

        def counting_iterdir(self_path):
            nonlocal iterdir_count
            iterdir_count += 1
            return original_iterdir(self_path)

        # Run 10 different patterns through _find_matching_directories
        patterns = [
            "**/*.py",
            "**/*.md",
            "dir_00/**",
            "dir_01/*.py",
            "*.py",
            "dir_1*/*.py",
            "**/*.txt",
            "dir_05/**/*.py",
            "**/*_0.py",
            "dir_19/*.py",
        ]

        with patch.object(Path, "iterdir", counting_iterdir):
            for pattern in patterns:
                optimizer._pattern_cache.pop(pattern, None)
                optimizer._find_matching_directories(pattern)

        assert iterdir_count == 0, (
            f"Expected 0 iterdir calls with index-based lookup, got {iterdir_count}. "
            f"10 patterns x 20 directories would have caused 200 iterdir calls "
            f"in the pre-optimization implementation."
        )

    def test_no_iterdir_scaling_with_pollution_candidates(self, tmp_path: Path) -> None:
        """With C candidate directories, zero iterdir calls in pollution scoring.

        Pre-optimization: C iterdir calls in _calculate_inheritance_pollution.
        Post-optimization: 0 iterdir calls (all from in-memory index).
        """
        # Create tree with parent + 15 children
        for i in range(15):
            _touch(tmp_path, f"src/child_{i:02d}/file.py")
        _touch(tmp_path, "src/app.py")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _make_instruction(apply_to="**/*.py")
        optimizer.optimize_instruction_placement([instruction])

        iterdir_count = 0
        original_iterdir = Path.iterdir

        def counting_iterdir(self_path):
            nonlocal iterdir_count
            iterdir_count += 1
            return original_iterdir(self_path)

        src_dir = tmp_path / "src"
        with patch.object(Path, "iterdir", counting_iterdir):
            # Call pollution scoring multiple times (simulating candidate evaluation)
            for _ in range(10):
                optimizer._calculate_inheritance_pollution(src_dir, "**/*.py")

        assert iterdir_count == 0, (
            f"Expected 0 iterdir calls with index-based lookup, got {iterdir_count}. "
            f"10 candidate evaluations would have caused 10 iterdir calls "
            f"in the pre-optimization implementation."
        )
