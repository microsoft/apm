"""Regression coverage for ContextOptimizer behavior tracked under #871.

Covers:
- ``_cached_glob`` reusing cached results across repeated calls without
  re-invoking the underlying ``glob.glob`` (cache layer populated via
  ``_glob_cache``).
- Lowest-common-ancestor placement when matches share a deep subtree, for
  both placement strategies that can route through the LCA helper:
    * ``_optimize_selective_placement`` (medium distribution, 0.3-0.7).
    * ``_optimize_single_point_placement`` (low distribution, < 0.3).
"""

import glob as glob_module
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.output.formatters import RICH_AVAILABLE, CompilationFormatter
from apm_cli.output.models import OptimizationDecision, PlacementStrategy
from apm_cli.primitives.models import Instruction, PrimitiveCollection


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def _make_high_distribution_apps_project(base: Path) -> None:
    for index in range(8):
        _touch(base, f"apps/bar/package-{index}/src/file-{index}.ts")
    _touch(base, "apps/foo/src/file.ts")
    _touch(base, "docs/readme.md")
    _touch(base, "README.md")


def _bar_instruction(placement: object | None = None) -> Instruction:
    return Instruction(
        name="bar-standards",
        file_path=Path("bar.instructions.md"),
        description="Bar app standards",
        apply_to="apps/bar/**",
        content="Bar standards",
        placement=placement,
    )


class TestCachedGlobUsesFileList:
    """Verify _cached_glob caches results and skips re-scanning the filesystem."""

    def test_cached_glob_caches_results(self, tmp_path: Path) -> None:
        """Second call with same pattern reuses ``_glob_cache``.

        Regression coverage for the cache layer added in #871: once a pattern
        has been resolved, subsequent calls must hit the cache and never
        re-invoke ``glob.glob`` for the same pattern.
        """
        (tmp_path / "a.py").touch()
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        with patch(
            "apm_cli.compilation.context_optimizer.glob.glob",
            wraps=glob_module.glob,
        ) as glob_spy:
            first = optimizer._cached_glob("**/*.py")
            second = optimizer._cached_glob("**/*.py")

        assert first == second
        assert "**/*.py" in optimizer._glob_cache
        assert first == optimizer._glob_cache["**/*.py"]
        # No-rescan guarantee: glob.glob must run exactly once for the pattern.
        assert glob_spy.call_count == 1, (
            f"expected exactly one glob.glob invocation, got {glob_spy.call_count}"
        )


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


class TestInstructionPlacementOverride:
    """Tests for per-instruction ``placement:`` frontmatter overrides."""

    def test_absent_placement_preserves_high_distribution_root_placement(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction()])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve() == tmp_path.resolve()
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.DISTRIBUTED

    def test_subdirectory_override_places_high_distribution_pattern_at_lca(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction("subdirectory")])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve().relative_to(tmp_path.resolve()).as_posix() == "apps/bar"
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.MANUAL_OVERRIDE

    def test_root_override_places_instruction_at_project_root(self, tmp_path: Path) -> None:
        _make_high_distribution_apps_project(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction("root")])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve() == tmp_path.resolve()
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.MANUAL_OVERRIDE

    def test_explicit_path_override_places_instruction_at_valid_directory(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction("apps/bar")])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve().relative_to(tmp_path.resolve()).as_posix() == "apps/bar"
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.MANUAL_OVERRIDE

    def test_invalid_explicit_path_overrides_warn_and_fall_back_to_auto(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        _touch(tmp_path, "README.md")
        cases = [
            (str(tmp_path.parent / "outside"), "absolute paths are not allowed"),
            ("../outside", "traversal sequence"),
            ("%2e%2e/outside", "traversal sequence"),
            ("apps/missing", "path does not exist"),
            ("README.md", "path is not a directory"),
            ("apps/foo", "path does not cover all directories matched by applyTo"),
        ]

        for placement, warning_fragment in cases:
            optimizer = ContextOptimizer(base_dir=str(tmp_path))

            result = optimizer.optimize_instruction_placement([_bar_instruction(placement)])

            assert len(result) == 1
            placement_dir = next(iter(result.keys()))
            assert placement_dir.resolve() == tmp_path.resolve()
            assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.DISTRIBUTED
            assert any(
                warning_fragment in warning and "Falling back to automatic placement" in warning
                for warning in optimizer._warnings
            )

    def test_non_string_placement_override_warns_and_falls_back_to_auto(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction(True)])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve() == tmp_path.resolve()
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.DISTRIBUTED
        assert any(
            "placement must be a string" in warning
            and "Falling back to automatic placement" in warning
            for warning in optimizer._warnings
        )

    def test_symlink_placement_override_outside_project_warns_and_falls_back(
        self, tmp_path: Path
    ) -> None:
        _make_high_distribution_apps_project(tmp_path)
        outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
        outside_dir.mkdir()
        symlink_path = tmp_path / "apps" / "escape"
        try:
            symlink_path.symlink_to(outside_dir, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is not available: {exc}")
        optimizer = ContextOptimizer(base_dir=str(tmp_path))

        result = optimizer.optimize_instruction_placement([_bar_instruction("apps/escape")])

        assert len(result) == 1
        placement_dir = next(iter(result.keys()))
        assert placement_dir.resolve() == tmp_path.resolve()
        assert optimizer._optimization_decisions[-1].strategy == PlacementStrategy.DISTRIBUTED
        assert any(
            "outside the allowed base directory" in warning
            and "Falling back to automatic placement" in warning
            for warning in optimizer._warnings
        )

    def test_compile_dry_run_reports_manual_override(self, tmp_path: Path) -> None:
        _make_high_distribution_apps_project(tmp_path)
        primitives = PrimitiveCollection()
        primitives.instructions.append(_bar_instruction("subdirectory"))
        logger = _RecordingLogger()

        result = AgentsCompiler(str(tmp_path)).compile(
            CompilationConfig(target="agents", dry_run=True, debug=True),
            primitives,
            logger=logger,
        )

        assert result.success
        assert "apps/bar/AGENTS.md" in result.content
        assert "Manual Override" in "\n".join(logger.messages)

    @pytest.mark.skipif(not RICH_AVAILABLE, reason="Rich output is not available")
    def test_verbose_formatter_reports_absolute_root_placement_as_root_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        formatter = CompilationFormatter(use_color=True)
        decision = OptimizationDecision(
            instruction=_bar_instruction(),
            pattern="apps/bar/**",
            matching_directories=8,
            total_directories=10,
            distribution_score=0.5,
            strategy=PlacementStrategy.SELECTIVE_MULTI,
            placement_directories=[tmp_path],
            reasoning="Root fallback placement",
        )

        output = "\n".join(formatter._format_mathematical_analysis([decision]))

        assert "Root -> All files" in output
        assert "inherit" in output


class _RecordingLogger:
    """Collect compiler output emitted through CommandLogger-compatible methods."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __getattr__(self, _name: str) -> Callable[..., None]:
        def record(message: str = "", **_kwargs: object) -> None:
            if message:
                self.messages.append(str(message))

        return record
