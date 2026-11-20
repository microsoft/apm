"""Behavioral receipts for OpenAPM target requirements (req-tg-*).

Per-target parameters derive from KNOWN_TARGETS; no target name is
hard-coded except the spec's own explicit-only list and the Claude hook
contract of req-tg-010.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.core.target_detection import parse_target_field
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, active_targets
from tests.unit.integration.test_hook_integrator_defect_regression import (
    _package_info,
    _session_start_hook,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("req-tg-001", "req-tg-004", "req-tg-010")]

_DETECTABLE = sorted(name for name, profile in KNOWN_TARGETS.items() if profile.detect_by_dir)
_EXPLICIT_ONLY = sorted(
    name for name, profile in KNOWN_TARGETS.items() if profile.capability.explicit_only
)


def _names(profiles) -> list[str]:
    return sorted(profile.name for profile in profiles)


# --- req-tg-001: detection only through the registered predicate ------------


@pytest.mark.req("req-tg-001")
@pytest.mark.parametrize("name", _DETECTABLE)
def test_target_is_detected_only_by_its_own_root(tmp_path: Path, name: str) -> None:
    """Creating one target's root activates that target and no other."""
    root_dir = KNOWN_TARGETS[name].root_dir
    (tmp_path / root_dir).mkdir(parents=True)
    expected = sorted(n for n in _DETECTABLE if KNOWN_TARGETS[n].root_dir == root_dir)

    assert _names(active_targets(tmp_path, create_config=False)) == expected


@pytest.mark.req("req-tg-001")
def test_explicit_only_targets_are_never_auto_detected_or_in_all(tmp_path: Path) -> None:
    """agent-skills, antigravity, hermes and grok-bot need an explicit selection."""
    spec_explicit_only = ["agent-skills", "antigravity", "grok-bot", "hermes"]
    assert set(spec_explicit_only) <= set(_EXPLICIT_ONLY)
    for name in _EXPLICIT_ONLY:
        (tmp_path / KNOWN_TARGETS[name].root_dir).mkdir(parents=True, exist_ok=True)

    detected = set(_names(active_targets(tmp_path, create_config=False)))
    expanded = set(_names(active_targets(tmp_path, "all", create_config=False)))
    assert detected.isdisjoint(_EXPLICIT_ONLY)
    assert expanded.isdisjoint(_EXPLICIT_ONLY)
    for name in spec_explicit_only:
        assert _names(active_targets(tmp_path, name, create_config=False)) == [name]


# --- req-tg-004: vendor-extension target identifiers -------------------------


@pytest.mark.req("req-tg-004")
@pytest.mark.xfail(strict=True, reason="#4251: x-<vendor>-<name> targets are rejected at parse")
def test_vendor_target_identifier_parses(tmp_path: Path) -> None:
    assert parse_target_field("x-acme-foo") == "x-acme-foo"


# --- req-tg-010: Claude project hooks are anchored to the project -------------


def _claude_hook_project(tmp_path: Path, script: str) -> tuple[Path, Path]:
    project = tmp_path / "project"
    package = tmp_path / "hookpkg"
    hooks = package / "hooks"
    hooks.mkdir(parents=True)
    script_path = hooks / script
    script_path.write_text('#!/bin/sh\necho "ran:$1" > "$HOOK_OUT"\n', encoding="utf-8")
    script_path.chmod(0o755)
    (hooks / "hooks.json").write_text(
        json.dumps(_session_start_hook(f"./hooks/{script} start")), encoding="utf-8"
    )
    return project, package


@pytest.mark.req("req-tg-010")
def test_claude_hook_command_runs_from_outside_the_project(tmp_path: Path) -> None:
    """The deployed command uses $CLAUDE_PROJECT_DIR, embeds no checkout path, and runs."""
    project, package = _claude_hook_project(tmp_path, "run.sh")

    HookIntegrator().integrate_package_hooks_claude(_package_info(package, "hookpkg"), project)

    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "${CLAUDE_PROJECT_DIR}/.claude/hooks/hookpkg/hooks/run.sh" in command
    assert str(tmp_path) not in command and str(tmp_path.resolve()) not in command
    if sys.platform == "win32":
        pytest.skip("POSIX shell execution of the deployed command")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = tmp_path / "hook.out"
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "HOOK_OUT": str(out)}
    subprocess.run(["sh", "-c", command], cwd=elsewhere, env=env, check=True)
    assert out.read_text(encoding="utf-8").strip() == "ran:start"


@pytest.mark.req("req-tg-010")
@pytest.mark.parametrize("script", ["run$HOME.sh", "run`id`.sh"], ids=["dollar", "backtick"])
def test_claude_hook_path_with_shell_expansion_character_is_rejected(
    tmp_path: Path, script: str
) -> None:
    project, package = _claude_hook_project(tmp_path, script)

    with pytest.raises(ValueError, match="shell expansion characters"):
        HookIntegrator().integrate_package_hooks_claude(_package_info(package, "hookpkg"), project)
    settings = project / ".claude" / "settings.json"
    assert not settings.exists() or "run" not in settings.read_text(encoding="utf-8")
