"""Phase-3 unit tests for apm_cli.install.services — targeting uncovered branches.

Focuses on:
- _deployed_path_entry: RuntimeError when path outside project tree and no targets
- _deployed_path_entry: copilot-app path via second fallback loop
- integrate_package_primitives: early return when no targets
- integrate_package_primitives: scratch_root validation
- integrate_package_primitives: _format_target_collapse (0, 1, 2, 3+ paths, verbose)
- integrate_package_primitives: copilot-app path in log hint
- integrate_package_primitives: result shape with skills
- integrate_local_content: delegates to integrate_package_primitives
- integrate_local_bundle: dry_run, collision skip, deployed files
- backward compat aliases
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apm_cli.install.services import (
    IntegratorBundle,
    integrate_package_primitives,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.security.executables import AUTHORIZED_GATE_DISABLED, authorized_exec_map


def _to_bundle(d: dict) -> IntegratorBundle:
    """Convert a dict of old-style integrator kwargs to an IntegratorBundle."""
    return IntegratorBundle(
        prompt=d["prompt_integrator"],
        agent=d["agent_integrator"],
        skill=d["skill_integrator"],
        instruction=d["instruction_integrator"],
        command=d["command_integrator"],
        hook=d["hook_integrator"],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def inject_config(monkeypatch: pytest.MonkeyPatch):
    import apm_cli.config as _conf

    def _set(cfg: dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


def _make_cowork_target(cowork_root: Path) -> Any:
    return replace(KNOWN_TARGETS["copilot-cowork"], resolved_deploy_root=cowork_root)


def _make_copilot_app_target(app_root: Path) -> Any:
    return replace(KNOWN_TARGETS["copilot-app"], resolved_deploy_root=app_root)


def _make_integrators() -> dict[str, Any]:
    skill_result = MagicMock()
    skill_result.target_paths = []
    skill_result.skill_created = False
    skill_result.sub_skills_promoted = 0
    skill_result.bin_deployed = 0
    integrators: dict[str, Any] = {
        k: MagicMock()
        for k in [
            "prompt_integrator",
            "agent_integrator",
            "skill_integrator",
            "instruction_integrator",
            "command_integrator",
            "hook_integrator",
        ]
    }
    integrators["skill_integrator"].integrate_package_skill.return_value = skill_result
    return integrators


# ---------------------------------------------------------------------------
# _deployed_path_entry — RuntimeError when no matching target
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _deployed_path_entry — second fallback loop (no prior match from first loop)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_package_primitives — early return with no targets
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_package_primitives — scratch_root validation
# ---------------------------------------------------------------------------


class TestIntegratePackagePrimitivesScratchRoot:
    def test_invalid_scratch_root_fails_before_target_cleanup(self, tmp_path: Path) -> None:
        """A replay-boundary error must leave live hook bytes untouched."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        cursor_dir = project_root / ".cursor"
        cursor_dir.mkdir()
        config = cursor_dir / "hooks.json"
        original = b'{"version": 1, "hooks": {"Stop": []}}\n'
        config.write_bytes(original)
        package_root = project_root / "apm_modules" / "targeted-hooks"
        package_root.mkdir(parents=True)
        pkg_info = PackageInfo(
            package=APMPackage(
                name="targeted-hooks",
                version="1.0.0",
                target="claude",
            ),
            install_path=package_root,
        )
        integrators = _make_integrators()

        with pytest.raises((RuntimeError, ValueError)):
            integrate_package_primitives(
                pkg_info,
                project_root,
                skill_subset=None,
                targets=[KNOWN_TARGETS["cursor"]],
                diagnostics=MagicMock(),
                integrators=_to_bundle(integrators),
                force=False,
                managed_files=None,
                scratch_root=scratch,
                allow_executables=authorized_exec_map(AUTHORIZED_GATE_DISABLED),
            )

        assert config.read_bytes() == original
        integrators["hook_integrator"].reconcile_package_target_restriction.assert_not_called()


# ---------------------------------------------------------------------------
# integrate_package_primitives — _format_target_collapse
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_package_primitives — sub_skills_promoted logging
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_package_primitives — skill path outside project tree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_local_content
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_local_bundle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backward compat aliases
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate_package_primitives — copilot-app workflow hint
# ---------------------------------------------------------------------------
