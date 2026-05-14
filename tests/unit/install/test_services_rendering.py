"""Unit tests for per-dep rendering rules in ``services.integrate_package_primitives``.

Covers Workstream A:
* A2 -- 1/2/3+ multi-target collapse rule for non-skill primitives, plus
  ``--verbose`` expansion.
* A3 -- ``(files unchanged)`` warm-cache annotation when no primitives
  integrate any files for a dep.

These tests stub the integrators so we can observe exactly which
``logger.tree_item(...)`` lines the rendering code emits.  Mocking at
the integrator boundary keeps the test independent of dispatch /
target-detection internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_integrator_returning(
    files_per_target: list[int],
    adopted_per_target: list[int] | None = None,
) -> MagicMock:
    """Return a MagicMock whose integrate method returns
    sequential ``IntegrationResult``-like objects.

    Each call returns one entry from ``files_per_target`` -- so the
    first target sees ``files_per_target[0]`` files, etc.

    ``adopted_per_target`` mirrors ``files_per_target`` for the silent
    adopt counter; defaults to all zeros.
    """
    integrator = MagicMock()
    if adopted_per_target is None:
        adopted_per_target = [0] * len(files_per_target)
    results = []
    for n, a in zip(files_per_target, adopted_per_target, strict=False):
        r = MagicMock()
        r.files_integrated = n
        r.files_adopted = a
        r.links_resolved = 0
        r.target_paths = []
        results.append(r)
    integrator.integrate_agents_for_target = MagicMock(side_effect=results)
    return integrator


def _zero_skill_result() -> MagicMock:
    skill_result = MagicMock()
    skill_result.target_paths = []
    skill_result.skill_created = False
    skill_result.sub_skills_promoted = 0
    return skill_result


def _make_pkg_info(tmp_path: Path) -> MagicMock:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / ".apm").mkdir(exist_ok=True)
    pkg = MagicMock()
    pkg.install_path = pkg_dir
    pkg.name = "test-pkg"
    return pkg


def _integrator_kwargs(prompt_integrator: MagicMock) -> dict[str, Any]:
    skill_integrator = MagicMock()
    skill_integrator.integrate_package_skill.return_value = _zero_skill_result()
    return {
        "prompt_integrator": MagicMock(),
        "agent_integrator": prompt_integrator,
        "skill_integrator": skill_integrator,
        "instruction_integrator": MagicMock(),
        "command_integrator": MagicMock(),
        "hook_integrator": MagicMock(),
    }


def _prompts_only_dispatch() -> dict[str, Any]:
    """Return a one-entry dispatch table with just 'agents' so the test
    does not need to stub all the other primitive integrators.

    Agents deploy to copilot, claude, cursor, codex (4 of 5 KNOWN_TARGETS),
    which gives us enough multi-target coverage for the 1/2/3+/5 collapse
    rule without having to special-case hooks.
    """
    from apm_cli.integration.agent_integrator import AgentIntegrator
    from apm_cli.integration.dispatch import PrimitiveDispatch

    return {
        "agents": PrimitiveDispatch(
            AgentIntegrator,
            "integrate_agents_for_target",
            "sync_for_target",
            "agents",
        ),
    }


def _logger_lines(logger: MagicMock) -> list[str]:
    """Extract all tree_item lines from a mock logger."""
    return [c.args[0] for c in logger.tree_item.call_args_list]


def _ctx(verbose: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.cowork_nonsupported_warned = False
    ctx.verbose = verbose
    return ctx


# ---------------------------------------------------------------------------
# A2 -- 1 / 2 / 3+ collapse rule and --verbose expansion
# ---------------------------------------------------------------------------


class TestMultiTargetCollapseRule:
    """Per-primitive aggregation: one line per kind, regardless of #targets."""

    def _run(
        self,
        tmp_path: Path,
        n_targets: int,
        files_per_target: list[int],
        verbose: bool = False,
    ) -> list[str]:
        from apm_cli.install.services import integrate_package_primitives

        # Build N distinct project-style targets from the canonical set.
        target_pool = ["copilot", "claude", "cursor", "codex"]
        targets = [KNOWN_TARGETS[name] for name in target_pool[:n_targets]]
        prompt_integrator = _make_integrator_returning(files_per_target)
        kwargs = _integrator_kwargs(prompt_integrator)
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(verbose=verbose),
                force=False,
                managed_files=None,
                **kwargs,
            )

        return _logger_lines(logger)

    def test_single_target_emits_path(self, tmp_path: Path) -> None:
        lines = self._run(tmp_path, n_targets=1, files_per_target=[3])
        # One aggregate line, "3 agents integrated -> <path>"
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        assert prompt_lines[0].startswith("  |-- 3 agents integrated -> ")
        assert "," not in prompt_lines[0]
        assert "targets" not in prompt_lines[0]

    def test_two_targets_emits_comma_separated(self, tmp_path: Path) -> None:
        lines = self._run(tmp_path, n_targets=2, files_per_target=[2, 3])
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        # files aggregated 2+3 = 5
        assert prompt_lines[0].startswith("  |-- 5 agents integrated -> ")
        # Two paths, comma separated, no "N targets" collapse
        assert prompt_lines[0].count(",") == 1
        assert " targets" not in prompt_lines[0]

    def test_three_or_more_targets_collapses_to_count(self, tmp_path: Path) -> None:
        lines = self._run(tmp_path, n_targets=3, files_per_target=[1, 2, 4])
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        assert prompt_lines[0] == "  |-- 7 agents integrated -> 3 targets"

    def test_four_targets_collapses_to_count(self, tmp_path: Path) -> None:
        lines = self._run(tmp_path, n_targets=4, files_per_target=[1, 1, 1, 1])
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        assert prompt_lines[0] == "  |-- 4 agents integrated -> 4 targets"

    def test_verbose_expands_full_target_list(self, tmp_path: Path) -> None:
        lines = self._run(tmp_path, n_targets=3, files_per_target=[1, 2, 4], verbose=True)
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        # First line is the aggregate header (no "-> ..."); per-target lines
        # follow as "  |     -> <path>".
        assert prompt_lines[0] == "  |-- 7 agents integrated:"
        expansion = [ln for ln in lines if ln.startswith("  |     -> ")]
        assert len(expansion) == 3

    def test_targets_with_zero_files_excluded_from_paths(self, tmp_path: Path) -> None:
        # Three targets, but only the second one actually integrates files.
        lines = self._run(tmp_path, n_targets=3, files_per_target=[0, 5, 0])
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        # 5 files, single contributing target -- no commas, no "N targets".
        assert prompt_lines[0].startswith("  |-- 5 agents integrated -> ")
        assert "," not in prompt_lines[0]


# ---------------------------------------------------------------------------
# Adopted-file visibility -- the install summary must show silent adopts
# ---------------------------------------------------------------------------


class TestAdoptedFileVisibility:
    """Pre-fix the adopt branch was invisible. In an adopt-only run the
    install summary printed nothing and looked like a no-op even though
    the lockfile WAS being repopulated. These tests lock in the new
    visibility contract: adopt counts surface in the per-kind line.
    """

    def _run(
        self,
        tmp_path: Path,
        files_per_target: list[int],
        adopted_per_target: list[int],
    ) -> list[str]:
        from apm_cli.install.services import integrate_package_primitives

        target_pool = ["copilot", "claude", "cursor", "codex"]
        targets = [KNOWN_TARGETS[name] for name in target_pool[: len(files_per_target)]]
        integrator = _make_integrator_returning(files_per_target, adopted_per_target)
        kwargs = _integrator_kwargs(integrator)
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(),
                force=False,
                managed_files=None,
                **kwargs,
            )
        return _logger_lines(logger)

    def test_adopt_only_run_emits_summary_line(self, tmp_path: Path) -> None:
        """The catch-22 reproducer: lockfile wiped, files still on disk
        byte-identical to source. Pre-fix: zero summary lines for the
        kind. Post-fix: ``N agents adopted -> <path>``.
        """
        lines = self._run(tmp_path, files_per_target=[0], adopted_per_target=[3])
        prompt_lines = [ln for ln in lines if "agents adopted" in ln]
        assert len(prompt_lines) == 1, (
            "Adopt-only run must still emit a per-kind summary line; "
            "previously the line was suppressed and the install looked like a no-op."
        )
        assert prompt_lines[0].startswith("  |-- 3 agents adopted -> ")

    def test_mixed_integrate_and_adopt_emits_combined_line(self, tmp_path: Path) -> None:
        """Half the files freshly written, half adopted. The line must
        lead with the integrated count and append the adopt count in
        parens so the two phases are individually visible.
        """
        lines = self._run(tmp_path, files_per_target=[2], adopted_per_target=[3])
        prompt_lines = [ln for ln in lines if "agents integrated" in ln]
        assert len(prompt_lines) == 1
        assert "(3 adopted)" in prompt_lines[0]
        assert prompt_lines[0].startswith("  |-- 2 agents integrated (3 adopted) -> ")

    def test_no_work_emits_no_line(self, tmp_path: Path) -> None:
        """Belt-and-braces: zero integrated AND zero adopted -> still
        no line (warm-cache annotation is a separate concern handled
        by TestWarmCacheAnnotation).
        """
        lines = self._run(tmp_path, files_per_target=[0], adopted_per_target=[0])
        assert not [ln for ln in lines if "agents integrated" in ln or "agents adopted" in ln]


# ---------------------------------------------------------------------------
# A3 -- (files unchanged) annotation when nothing was integrated
# ---------------------------------------------------------------------------


class TestWarmCacheAnnotation:
    """A3: emit one annotation when total integration is zero."""

    def test_emits_annotation_when_no_files_integrated(self, tmp_path: Path) -> None:
        from apm_cli.install.services import integrate_package_primitives

        targets = [KNOWN_TARGETS["copilot"]]
        prompt_integrator = _make_integrator_returning([0])  # zero files
        kwargs = _integrator_kwargs(prompt_integrator)
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(),
                force=False,
                managed_files=None,
                **kwargs,
            )

        lines = _logger_lines(logger)
        assert "  |-- (files unchanged)" in lines

    def test_no_annotation_when_files_integrated(self, tmp_path: Path) -> None:
        from apm_cli.install.services import integrate_package_primitives

        targets = [KNOWN_TARGETS["copilot"]]
        prompt_integrator = _make_integrator_returning([2])
        kwargs = _integrator_kwargs(prompt_integrator)
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(),
                force=False,
                managed_files=None,
                **kwargs,
            )

        lines = _logger_lines(logger)
        assert "  |-- (files unchanged)" not in lines

    def test_no_annotation_when_skill_created(self, tmp_path: Path) -> None:
        from apm_cli.install.services import integrate_package_primitives

        targets = [KNOWN_TARGETS["copilot"]]
        prompt_integrator = _make_integrator_returning([0])
        kwargs = _integrator_kwargs(prompt_integrator)
        # Override the skill integrator to report a skill was created.
        skill_result = MagicMock()
        skill_result.target_paths = []
        skill_result.skill_created = True
        skill_result.sub_skills_promoted = 0
        kwargs["skill_integrator"].integrate_package_skill.return_value = skill_result
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(),
                force=False,
                managed_files=None,
                **kwargs,
            )

        lines = _logger_lines(logger)
        assert "  |-- (files unchanged)" not in lines


# ---------------------------------------------------------------------------
# Smoke: aggregate counter equals the sum across targets
# ---------------------------------------------------------------------------


class TestAggregateCounterPreserved:
    def test_counter_equals_sum_across_targets(self, tmp_path: Path) -> None:
        from apm_cli.install.services import integrate_package_primitives

        targets = [KNOWN_TARGETS["copilot"], KNOWN_TARGETS["claude"]]
        prompt_integrator = _make_integrator_returning([3, 4])
        kwargs = _integrator_kwargs(prompt_integrator)
        pkg = _make_pkg_info(tmp_path)
        logger = MagicMock()

        with patch(
            "apm_cli.integration.dispatch.get_dispatch_table",
            return_value=_prompts_only_dispatch(),
        ):
            result = integrate_package_primitives(
                pkg,
                tmp_path,
                targets=targets,
                diagnostics=MagicMock(),
                package_name="test-pkg",
                logger=logger,
                ctx=_ctx(),
                force=False,
                managed_files=None,
                **kwargs,
            )

        assert result["agents"] == 7


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()
