"""Services-tier regression trap for the hook ``user_scope`` dispatch (#1394).

The fix for #1394 hinges on a single line in
``apm_cli.install.services.integrate_package_primitives``:

    if _prim_name == "hooks":
        _call_kwargs["user_scope"] = scope is InstallScope.USER

``test_hook_integrator.py`` exercises ``HookIntegrator`` directly,
which proves the integrator honours ``user_scope=True/False`` but
does NOT prove the services-tier dispatch translates ``InstallScope``
to that boolean correctly.  A future refactor that swapped the
``InstallScope`` constants, dropped the kwarg, or moved hook
dispatch to a different branch would silently restore the
portability regression with no failing test.

These tests close that gap by calling
``integrate_package_primitives`` through the production boundary
with a mocked ``hook_integrator`` and asserting the dispatched
``user_scope`` kwarg matches the supplied ``InstallScope``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.integration.skill_integrator import SkillIntegrationResult
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import DiagnosticCollector


@pytest.fixture(autouse=True)
def _reset_config_cache() -> Any:
    """Match the isolation pattern of tests/unit/install/test_services.py."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


def _claude_hooks_only_target() -> Any:
    """Return a Claude TargetProfile whose only primitive is ``hooks``.

    Keeping the dispatch surface to a single primitive guarantees the
    mocked ``hook_integrator`` is invoked exactly once, so the asserted
    ``user_scope`` kwarg is unambiguous.
    """
    claude = KNOWN_TARGETS["claude"]
    return replace(claude, primitives={"hooks": claude.primitives["hooks"]})


def _make_integrator_returning_empty() -> MagicMock:
    """Mock integrator that returns a real IntegrationResult.

    ``integrate_package_primitives`` reads ``files_integrated``,
    ``target_paths``, and ``links_resolved`` from the result; a real
    dataclass keeps assertions on those fields meaningful without
    leaking MagicMock auto-attribute behaviour into the counters.
    """
    integrator = MagicMock(name="hook_integrator")
    integrator.integrate_hooks_for_target.return_value = IntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
        links_resolved=0,
    )
    return integrator


def _make_skill_integrator() -> MagicMock:
    """Mock skill integrator returning a real IntegrationResult.

    ``integrate_package_primitives`` inspects ``sub_skills_promoted``,
    ``skill_created``, and ``target_paths`` on the skill result; a real
    dataclass keeps those reads typed (vs MagicMock auto-attribute).
    """
    skill = MagicMock(name="skill_integrator")
    skill.integrate_package_skill.return_value = SkillIntegrationResult(
        skill_created=False,
        skill_updated=False,
        skill_skipped=False,
        skill_path=None,
        references_copied=0,
        target_paths=[],
    )
    return skill


def _call(scope: InstallScope, project_root: Path) -> MagicMock:
    """Invoke ``integrate_package_primitives`` and return the hook integrator mock."""
    hook_integrator = _make_integrator_returning_empty()
    package_info = MagicMock(name="package_info")
    package_info.install_path = project_root
    integrate_package_primitives(
        package_info,
        project_root,
        targets=[_claude_hooks_only_target()],
        integrators=IntegratorBundle(
            prompt=MagicMock(),
            agent=MagicMock(),
            skill=_make_skill_integrator(),
            instruction=MagicMock(),
            command=MagicMock(),
            hook=hook_integrator,
        ),
        force=False,
        managed_files=None,
        diagnostics=DiagnosticCollector(),
        scope=scope,
    )
    return hook_integrator


def test_user_scope_dispatches_user_scope_true(tmp_path: Path) -> None:
    """``InstallScope.USER`` must reach the hook integrator as ``user_scope=True``.

    Regression trap for the production wiring at
    ``services.py`` ~L258.  A constant swap that mapped USER to
    ``user_scope=False`` would silently restore #1394 -- this test
    fails first.
    """
    hook_integrator = _call(InstallScope.USER, tmp_path)

    hook_integrator.integrate_hooks_for_target.assert_called_once()
    kwargs = hook_integrator.integrate_hooks_for_target.call_args.kwargs
    assert kwargs.get("user_scope") is True, (
        "InstallScope.USER must dispatch user_scope=True so user-scope "
        f"hook commands stay absolute (#1310/#1354); got kwargs={kwargs!r}"
    )


def test_project_scope_dispatches_user_scope_false(tmp_path: Path) -> None:
    """``InstallScope.PROJECT`` must reach the hook integrator as ``user_scope=False``.

    Regression trap for #1394: project-scope hooks MUST stay
    repo-relative so committed configs are portable across clones / CI.
    """
    hook_integrator = _call(InstallScope.PROJECT, tmp_path)

    hook_integrator.integrate_hooks_for_target.assert_called_once()
    kwargs = hook_integrator.integrate_hooks_for_target.call_args.kwargs
    assert kwargs.get("user_scope") is False, (
        "InstallScope.PROJECT must dispatch user_scope=False so committed "
        f"hook configs stay portable (#1394); got kwargs={kwargs!r}"
    )


def test_scope_none_defaults_user_scope_false(tmp_path: Path) -> None:
    """``scope=None`` (legacy call sites) must default to the safe project-scope path.

    The fix's invariant: anything that is not unambiguously
    ``InstallScope.USER`` must NOT absolutize hook commands.  A future
    refactor that flipped this default to ``True`` would re-introduce
    #1394 for every legacy call site at once.
    """
    hook_integrator = _call(None, tmp_path)  # type: ignore[arg-type]

    hook_integrator.integrate_hooks_for_target.assert_called_once()
    kwargs = hook_integrator.integrate_hooks_for_target.call_args.kwargs
    assert kwargs.get("user_scope") is False, (
        "scope=None must default to user_scope=False to preserve #1394 "
        f"safety for legacy call sites; got kwargs={kwargs!r}"
    )


def test_non_hook_integrators_never_receive_user_scope(tmp_path: Path) -> None:
    """Sibling integrators do not accept ``user_scope`` -- the kwarg must be hook-only.

    Threading ``user_scope`` into prompt/agent/command/instruction
    integrators would raise ``TypeError`` at runtime.  This test
    locks the per-primitive gate so a refactor that hoists the kwarg
    above the ``if _prim_name == "hooks":`` check fails loudly.
    """
    claude = KNOWN_TARGETS["claude"]
    # Use a target that exposes BOTH hooks and a non-hook primitive
    # (commands) so the dispatch loop runs for both.
    target = replace(
        claude,
        primitives={
            "hooks": claude.primitives["hooks"],
            "commands": claude.primitives["commands"],
        },
    )
    command_integrator = MagicMock(name="command_integrator")
    command_integrator.integrate_commands_for_target.return_value = IntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
        links_resolved=0,
    )
    hook_integrator = _make_integrator_returning_empty()
    package_info = MagicMock(name="package_info")
    package_info.install_path = tmp_path
    integrate_package_primitives(
        package_info,
        tmp_path,
        targets=[target],
        integrators=IntegratorBundle(
            prompt=MagicMock(),
            agent=MagicMock(),
            skill=_make_skill_integrator(),
            instruction=MagicMock(),
            command=command_integrator,
            hook=hook_integrator,
        ),
        force=False,
        managed_files=None,
        diagnostics=DiagnosticCollector(),
        scope=InstallScope.USER,
    )

    command_integrator.integrate_commands_for_target.assert_called_once()
    cmd_kwargs = command_integrator.integrate_commands_for_target.call_args.kwargs
    assert "user_scope" not in cmd_kwargs, (
        f"Non-hook integrators must not receive the user_scope kwarg; got kwargs={cmd_kwargs!r}"
    )
