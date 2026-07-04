"""Regression coverage for ContextOptimizer behavior tracked under #871.

Covers:
- ``_cached_glob`` reusing cached results across repeated calls without
  re-running the underlying scan (``_safe_recursive_glob``; cache layer
  populated via ``_glob_cache``).
- ``_safe_recursive_glob`` excluding ``node_modules`` and not following
  directory symlinks, so a pnpm-style symlink cycle cannot make
  compilation hang (regression for the recursive-glob hang).
- Lowest-common-ancestor placement when matches share a deep subtree, for
  both placement strategies that can route through the LCA helper:
    * ``_optimize_selective_placement`` (medium distribution, 0.3-0.7).
    * ``_optimize_single_point_placement`` (low distribution, < 0.3).
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


class TestCachedGlobUsesFileList:
    """Verify _cached_glob caches results and skips re-scanning the filesystem."""

    def test_cached_glob_caches_results(self, tmp_path: Path) -> None:
        """Second call with same pattern reuses ``_glob_cache``.

        Regression coverage for the cache layer added in #871: once a pattern
        has been resolved, subsequent calls must hit the cache and never
        re-run the underlying scan for the same pattern.
        """
        (tmp_path / "a.py").touch()
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        with patch.object(
            optimizer,
            "_safe_recursive_glob",
            wraps=optimizer._safe_recursive_glob,
        ) as scan_spy:
            first = optimizer._cached_glob("**/*.py")
            second = optimizer._cached_glob("**/*.py")

        assert first == second == ["a.py"]
        assert "**/*.py" in optimizer._glob_cache
        assert first == optimizer._glob_cache["**/*.py"]
        # No-rescan guarantee: the scan must run exactly once for the pattern.
        assert scan_spy.call_count == 1, f"expected exactly one scan, got {scan_spy.call_count}"

    def test_cached_glob_excludes_node_modules_and_symlink_cycles(self, tmp_path: Path) -> None:
        """A ``**`` pattern must not descend ``node_modules`` or follow symlinks.

        Regression for the recursive-glob hang: ``glob.glob(recursive=True)``
        follows directory symlinks and descends excluded trees, so a pnpm-style
        ``node_modules`` (a symlink forest with cycles) made ``apm compile``
        walk an unbounded path space and never terminate. Filtering the
        ``os.walk``-based project file list (no symlink following, excluded
        dirs pruned) makes the cycle below harmless and drops ``node_modules``.
        """
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").touch()

        pkg = tmp_path / "node_modules" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "index.ts").touch()
        try:
            # pnpm-style self-referential symlink -> infinite loop if followed.
            (pkg / "loop").symlink_to(tmp_path / "node_modules", target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation not supported on this platform")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        matches = optimizer._cached_glob("**/*.ts")  # must terminate

        assert "src/app.ts" in matches
        assert not any(m.startswith("node_modules") for m in matches)


class TestUniversalApplyToFastPath:
    """Regression coverage for universal ``applyTo: "**"`` placement."""

    def test_universal_apply_to_reuses_directory_analysis(self, tmp_path: Path) -> None:
        for i in range(30):
            _touch(tmp_path, f"pkg{i}/file.txt")
        (tmp_path / "empty").mkdir()

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        expected_directories = {tmp_path / f"pkg{i}" for i in range(30)}
        instruction = Instruction(
            name="global-standards",
            file_path=Path("global.instructions.md"),
            description="Global coding standards",
            apply_to=" ** ",
            content="Global standards",
        )

        with (
            patch.object(
                optimizer,
                "_file_matches_pattern",
                wraps=optimizer._file_matches_pattern,
            ) as file_match_spy,
            patch.object(optimizer, "_cached_glob", wraps=optimizer._cached_glob) as glob_spy,
        ):
            result = optimizer.optimize_instruction_placement([instruction])

        assert list(result) == [tmp_path]
        assert optimizer._optimization_decisions[0].matching_directories == 30
        assert file_match_spy.call_count == 0
        assert glob_spy.call_count == 0
        assert optimizer._pattern_cache["**"] == expected_directories
        assert " ** " not in optimizer._pattern_cache
        for directory, analysis in optimizer._directory_cache.items():
            assert directory in optimizer._pattern_cache["**"]
            assert analysis.pattern_matches["**"] == analysis.total_files
            assert " ** " not in analysis.pattern_matches


class TestSelectivePlacementNonRootLCA:
    """Regression test for medium-distribution placement at a non-root LCA.

    Fixture sizing puts the distribution ratio in the SELECTIVE_MULTI tier
    (0.3-0.7), so this exercises ``_optimize_selective_placement``. The
    corrected implementation routes selective placement through
    ``_find_minimal_coverage_placement`` (LCA), which must return the deepest
    covering directory -- ``Engine/Plugins`` in this case, not the project
    root.
    """

    def test_lca_placement_is_non_root_for_selective_distribution(self, tmp_path: Path) -> None:
        # 4 sibling dirs with files + 2 PCG leaves => 6 dirs-with-files,
        # matching = 2, ratio ~ 0.33 (lands in SELECTIVE_MULTI tier).
        for d in ("Source", "Content", "Config", "Docs"):
            (tmp_path / d).mkdir()
            _touch(tmp_path, f"{d}/keep.txt")

        _touch(tmp_path, "Engine/Plugins/PCG/Source/Foo.cpp")
        _touch(tmp_path, "Engine/Plugins/PCG/Source/Foo.h")
        _touch(tmp_path, "Engine/Plugins/PCGExtra/Source/Bar.cpp")
        _touch(tmp_path, "Engine/Plugins/PCGExtra/Source/Bar.h")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = Instruction(
            name="pcg-standards",
            file_path=Path("pcg.instructions.md"),
            description="PCG plugin coding standards",
            apply_to="Engine/Plugins/PCG*/**/*",
            content="PCG standards",
        )

        original = ContextOptimizer._optimize_selective_placement
        with patch.object(
            ContextOptimizer,
            "_optimize_selective_placement",
            autospec=True,
            side_effect=original,
        ) as selective_spy:
            result = optimizer.optimize_instruction_placement([instruction])

        assert selective_spy.called, (
            "expected SELECTIVE_MULTI tier to invoke _optimize_selective_placement"
        )
        assert len(result) == 1, f"expected single placement, got {result}"
        placement_dir = next(iter(result.keys()))

        assert placement_dir.resolve() != tmp_path.resolve(), (
            f"placement landed at project root instead of LCA: {placement_dir}"
        )
        rel = placement_dir.resolve().relative_to(tmp_path.resolve())
        assert rel.as_posix() == "Engine/Plugins", (
            f"expected LCA Engine/Plugins, got {rel.as_posix()}"
        )


class TestSinglePointPlacementNonRootLCA:
    """Regression test for low-distribution placement at a non-root LCA.

    Fixture sizing pushes the distribution ratio below 0.3 so dispatch
    routes through ``_optimize_single_point_placement`` (the SINGLE_POINT
    tier, lines 856-897 of ``context_optimizer.py``). Even in that tier,
    a narrow ``applyTo`` pattern whose matches sit deep inside the same
    subtree must collapse to the deepest covering directory -- here
    ``Engine/Plugins`` -- never to the project root.
    """

    def test_lca_placement_is_non_root_for_low_distribution(self, tmp_path: Path) -> None:
        # 6 sibling dirs with files + 2 PCG leaves => 8 dirs-with-files,
        # matching = 2, ratio = 0.25 (lands in SINGLE_POINT tier, < 0.3).
        for d in ("Source", "Content", "Config", "Docs", "Saved", "Intermediate"):
            (tmp_path / d).mkdir()
            _touch(tmp_path, f"{d}/keep.txt")

        _touch(tmp_path, "Engine/Plugins/PCG/Source/Foo.cpp")
        _touch(tmp_path, "Engine/Plugins/PCG/Source/Foo.h")
        _touch(tmp_path, "Engine/Plugins/PCGExtra/Source/Bar.cpp")
        _touch(tmp_path, "Engine/Plugins/PCGExtra/Source/Bar.h")

        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = Instruction(
            name="pcg-standards",
            file_path=Path("pcg.instructions.md"),
            description="PCG plugin coding standards",
            apply_to="Engine/Plugins/PCG*/**/*",
            content="PCG standards",
        )

        original = ContextOptimizer._optimize_single_point_placement
        with patch.object(
            ContextOptimizer,
            "_optimize_single_point_placement",
            autospec=True,
            side_effect=original,
        ) as single_point_spy:
            result = optimizer.optimize_instruction_placement([instruction])

        assert single_point_spy.called, (
            "expected SINGLE_POINT tier to invoke _optimize_single_point_placement"
        )
        assert len(result) == 1, f"expected single placement, got {result}"
        placement_dir = next(iter(result.keys()))

        assert placement_dir.resolve() != tmp_path.resolve(), (
            f"placement landed at project root instead of LCA: {placement_dir}"
        )
        rel = placement_dir.resolve().relative_to(tmp_path.resolve())
        assert rel.as_posix() == "Engine/Plugins", (
            f"expected LCA Engine/Plugins, got {rel.as_posix()}"
        )
