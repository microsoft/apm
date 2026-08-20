"""Regression coverage for native hook event normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_MAP,
    HookIntegrator,
    _emit_hook_event_diagnostics,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _package(
    tmp_path: Path,
    hooks: dict[str, list[dict[str, Any]]],
    *,
    name: str = "native-event-kit",
) -> PackageInfo:
    package_root = tmp_path / "packages" / name
    hooks_dir = package_root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "events.json").write_text(
        json.dumps({"hooks": hooks}, indent=2),
        encoding="utf-8",
    )
    return PackageInfo(
        package=APMPackage(name=name, version="1.0.0"),
        install_path=package_root,
    )


def _commands(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["hooks"][0]["command"] for entry in entries]


@pytest.mark.parametrize(
    ("target", "source_event", "native_event"),
    [
        ("copilot", "SessionStart", "sessionStart"),
        ("copilot", "sessionStart", "sessionStart"),
        ("copilot", "Stop", "agentStop"),
        ("copilot", "AgentStop", "agentStop"),
        ("copilot", "agentStop", "agentStop"),
        ("claude", "SessionStart", "SessionStart"),
        ("claude", "sessionStart", "SessionStart"),
        ("claude", "Stop", "Stop"),
        ("claude", "AgentStop", "Stop"),
        ("claude", "agentStop", "Stop"),
    ],
)
def test_supported_aliases_map_to_documented_native_events(
    target: str,
    source_event: str,
    native_event: str,
) -> None:
    assert _HOOK_EVENT_MAP[target][source_event] == native_event


def test_unsupported_lowercase_stop_is_not_declared_as_native_alias() -> None:
    assert "stop" not in _HOOK_EVENT_MAP["copilot"]
    assert "stop" not in _HOOK_EVENT_MAP["claude"]


def test_copilot_alias_collisions_merge_in_source_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = _package(
        tmp_path,
        {
            "SessionStart": [_entry("echo start-pascal")],
            "sessionStart": [_entry("echo start-camel")],
            "Stop": [_entry("echo stop")],
            "AgentStop": [_entry("echo agent-pascal")],
            "agentStop": [_entry("echo agent-camel")],
        },
    )

    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"],
        package,
        project,
    )

    target = project / ".github" / "hooks" / "native-event-kit-events.json"
    hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["sessionStart", "agentStop"]
    assert _commands(hooks["sessionStart"]) == [
        "echo start-pascal",
        "echo start-camel",
    ]
    assert _commands(hooks["agentStop"]) == [
        "echo stop",
        "echo agent-pascal",
        "echo agent-camel",
    ]


def test_claude_alias_collisions_merge_in_source_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = _package(
        tmp_path,
        {
            "sessionStart": [_entry("echo start-camel")],
            "SessionStart": [_entry("echo start-pascal")],
            "agentStop": [_entry("echo agent-camel")],
            "AgentStop": [_entry("echo agent-pascal")],
            "Stop": [_entry("echo stop")],
        },
    )

    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["claude"],
        package,
        project,
    )

    hooks = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["SessionStart", "Stop"]
    assert _commands(hooks["SessionStart"]) == [
        "echo start-camel",
        "echo start-pascal",
    ]
    assert _commands(hooks["Stop"]) == [
        "echo agent-camel",
        "echo agent-pascal",
        "echo stop",
    ]


def test_unknown_event_warns_and_is_not_renamed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    package = _package(
        tmp_path,
        {"UnsupportedEvent": [_entry("echo unsupported")]},
    )

    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"],
        package,
        project,
    )

    warning = capsys.readouterr().out
    assert "UnsupportedEvent" in warning
    assert "may not be recognized" in warning
    assert "Rename events" in warning
    assert "then reinstall" in warning
    target = project / ".github" / "hooks" / "native-event-kit-events.json"
    hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["UnsupportedEvent"]


def test_supported_alias_diagnostics_do_not_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    aliases = ["SessionStart", "sessionStart", "Stop", "AgentStop", "agentStop"]

    for target in ("copilot", "claude"):
        _emit_hook_event_diagnostics(aliases, target, _HOOK_EVENT_MAP[target])

    assert "may not be recognized" not in capsys.readouterr().out


def test_reinstall_repairs_owned_stop_without_touching_user_hook(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = _package(
        tmp_path,
        {
            "SessionStart": [_entry("echo start")],
            "Stop": [_entry("echo managed-stop")],
        },
    )
    integrator = HookIntegrator()
    target = project / ".github" / "hooks" / "native-event-kit-events.json"
    user_target = project / ".github" / "hooks" / "user-hooks.json"
    user_payload = {
        "version": 1,
        "hooks": {"stop": [{"type": "command", "command": "echo user-stop"}]},
    }

    integrator.integrate_hooks_for_target(KNOWN_TARGETS["copilot"], package, project)
    stale = json.loads(target.read_text(encoding="utf-8"))
    stale["hooks"]["stop"] = stale["hooks"].pop("agentStop")
    target.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
    user_target.write_text(json.dumps(user_payload, indent=2) + "\n", encoding="utf-8")

    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"],
        package,
        project,
        managed_files={".github/hooks/native-event-kit-events.json"},
    )

    repaired = json.loads(target.read_text(encoding="utf-8"))
    assert set(repaired["hooks"]) == {"sessionStart", "agentStop"}
    assert "stop" not in repaired["hooks"]
    assert json.loads(user_target.read_text(encoding="utf-8")) == user_payload
