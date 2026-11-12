"""Regression coverage for the ContextOptimizer placement perf fix (#2969)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.compilation import context_optimizer
from apm_cli.compilation.context_optimizer import ContextOptimizer
from apm_cli.primitives.models import Instruction


def _touch(base: Path, rel: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def _instruction(apply_to: str) -> Instruction:
    return Instruction(
        name="i",
        file_path=Path("i.instructions.md"),
        description="d",
        apply_to=apply_to,
        content="c",
    )


def _analyzed(tmp_path: Path, files: list[str]) -> ContextOptimizer:
    for rel in files:
        _touch(tmp_path, rel)
    optimizer = ContextOptimizer(base_dir=str(tmp_path))
    optimizer._analyze_project_structure()
    return optimizer


class TestCachesResetBetweenCalls:
    def test_rel_path_cache_entry_does_not_survive(self, tmp_path: Path) -> None:
        _touch(tmp_path, "vendor/Button.ts")
        link = tmp_path / "src" / "Button.ts"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(tmp_path / "vendor" / "Button.ts")
        except OSError:
            pytest.skip("symlink creation not supported on this platform")
        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _instruction("**/*.ts")

        optimizer.optimize_instruction_placement([instruction])
        optimizer._rel_path_cache[link] = ("somewhere/else.ts", True)
        optimizer.optimize_instruction_placement([instruction])

        assert optimizer._rel_path_cache[link] == ("vendor/Button.ts", True)

    def test_resolved_dir_cache_entry_does_not_survive(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/a.py")
        optimizer = ContextOptimizer(base_dir=str(tmp_path))
        instruction = _instruction("**/*.py")
        stale_key = tmp_path / "nonexistent"

        optimizer.optimize_instruction_placement([instruction])
        optimizer._resolved_dir_cache[stale_key] = tmp_path.resolve()
        optimizer.optimize_instruction_placement([instruction])

        assert stale_key not in optimizer._resolved_dir_cache


class TestNoRepeatedWork:
    def test_depth_mean_computed_once(self, tmp_path: Path, monkeypatch) -> None:
        def sum_calls(dir_count: int) -> int:
            base = tmp_path / str(dir_count)
            optimizer = _analyzed(base, [f"d{i}/f.py" for i in range(dir_count)])
            calls = 0

            def counting_sum(*args):
                nonlocal calls
                calls += 1
                return sum(*args)

            monkeypatch.setattr(context_optimizer, "sum", counting_sum, raising=False)
            optimizer._calculate_distribution_score(set(optimizer._directory_cache))
            monkeypatch.delattr(context_optimizer, "sum")
            return calls

        assert sum_calls(3) == sum_calls(30)

    def test_candidate_check_stops_at_first_uncovered_directory(self, tmp_path: Path) -> None:
        optimizer = _analyzed(tmp_path, [f"src/d{i}/f.py" for i in range(10)] + ["other/f.py"])
        matching = {d for d in optimizer._directory_cache if d.parent.name == "src"}
        candidate = SimpleNamespace(
            directory=(tmp_path / "other").resolve(),
            coverage_efficiency=1.0,
            pollution_score=0.0,
        )

        with (
            patch.object(optimizer, "_generate_all_candidates", return_value=[candidate]),
            patch.object(
                optimizer,
                "_is_hierarchically_covered",
                wraps=optimizer._is_hierarchically_covered,
            ) as covered,
        ):
            optimizer._optimize_single_point_placement(matching, _instruction("src/**/*.py"))

        assert covered.call_count == 1

    def test_file_resolved_once_across_patterns(self, tmp_path: Path) -> None:
        optimizer = _analyzed(tmp_path, ["src/a.py"])
        file_path = (tmp_path / "src" / "a.py").resolve()

        with patch.object(Path, "resolve", autospec=True, side_effect=Path.resolve) as resolve:
            for pattern in ("**/*.py", "src/**/*.py", "*.py", "src/*.py"):
                optimizer._file_matches_pattern(file_path, pattern)

        assert [c.args[0] for c in resolve.call_args_list].count(file_path) == 1

    def test_directory_resolved_once_per_call(self, tmp_path: Path) -> None:
        optimizer = _analyzed(tmp_path, ["src/a/f.py", "src/b/f.py"])
        keys = set(optimizer._directory_cache)
        src = tmp_path / "src"

        with patch.object(Path, "resolve", autospec=True, side_effect=Path.resolve) as resolve:
            for _ in range(2):
                for key in keys:
                    optimizer._is_hierarchically_covered(key, src)
                    optimizer._is_instruction_relevant(_instruction("src/**/*.py"), key)
                optimizer._find_minimal_coverage_placement(keys)

        resolved = [c.args[0] for c in resolve.call_args_list]
        assert all(resolved.count(path) == 1 for path in [*keys, src])
