"""Native event spelling is part of the invocation contract, not presentation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = pytest.mark.component


def _package(tmp_path: Path, hooks: dict) -> PackageInfo:
    package_root = tmp_path / "packages" / "native-event-kit"
    hooks_dir = package_root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "events.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return PackageInfo(
        package=APMPackage(name="native-event-kit", version="1.0.0"),
        install_path=package_root,
    )


@pytest.mark.parametrize(
    "event",
    [
        "SessionStart",
        "sessionStart",
        "Stop",
        "agentStop",
        "UserPromptSubmit",
        "userPromptSubmitted",
        "PreToolUse",
        "preToolUse",
        "SubagentStop",
        "subagentStop",
    ],
)
def test_copilot_preserves_documented_native_event_spelling(tmp_path: Path, event: str) -> None:
    package = _package(tmp_path, {event: [{"type": "command", "command": "echo harmless"}]})
    project = tmp_path / "project"
    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["copilot"], package, project)
    path = project / ".github/hooks/native-event-kit-events.json"
    assert json.loads(path.read_text())["hooks"] == {
        event: [{"type": "command", "command": "echo harmless"}]
    }


@pytest.mark.parametrize(
    "target,event",
    [
        ("claude", "subagentStop"),
        ("claude", "sessionStart"),
        ("claude", "agentStop"),
        ("claude", "AgentStop"),
        ("copilot", "AgentStop"),
        ("copilot", "userPromptSubmit"),
        ("copilot", "UnsupportedEvent"),
        ("claude", "UnsupportedEvent"),
    ],
)
def test_unverified_alias_fails_without_native_writes(
    tmp_path: Path, target: str, event: str
) -> None:
    package = _package(tmp_path, {event: [{"type": "command", "command": "echo harmless"}]})
    project = tmp_path / "project"
    with pytest.raises(HookContractError, match="unsupported"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS[target], package, project)
    assert not project.exists()


def test_copilot_aliases_with_identical_commands_still_have_distinct_payloads(
    tmp_path: Path,
) -> None:
    events = ["SessionStart", "sessionStart", "Stop", "agentStop"]
    package = _package(
        tmp_path, {event: [{"type": "command", "command": "echo harmless"}] for event in events}
    )
    project = tmp_path / "project"
    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["copilot"], package, project)
    hooks = json.loads((project / ".github/hooks/native-event-kit-events.json").read_text())[
        "hooks"
    ]
    assert list(hooks) == events
    assert all(len(entries) == 1 for entries in hooks.values())


def test_reinstall_repairs_only_owned_copilot_document(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = _package(tmp_path, {"Stop": [{"type": "command", "command": "echo managed-stop"}]})
    integrator = HookIntegrator()
    target = project / ".github/hooks/native-event-kit-events.json"
    user_target = project / ".github/hooks/user-hooks.json"
    user_payload = {"version": 1, "hooks": {"stop": [{"command": "echo personal"}]}}
    integrator.integrate_hooks_for_target(KNOWN_TARGETS["copilot"], package, project)
    stale = json.loads(target.read_text())
    stale["hooks"]["stop"] = stale["hooks"].pop("Stop")
    target.write_text(json.dumps(stale))
    user_target.write_text(json.dumps(user_payload))
    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"],
        package,
        project,
        managed_files={".github/hooks/native-event-kit-events.json"},
    )
    assert set(json.loads(target.read_text())["hooks"]) == {"Stop"}
    assert json.loads(user_target.read_text()) == user_payload


@pytest.mark.parametrize("collision", [False, True])
def test_copilot_multi_descriptor_dedup_preserves_bindings_and_skipped_owners(
    tmp_path: Path, collision: bool
) -> None:
    binding = {"type": "command", "command": "echo harmless", "matcher": "Bash"}
    package = _package(tmp_path, {"PreToolUse": [binding]})
    extra = package.install_path / ".apm/hooks/extra.json"
    extra.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [binding, {**binding, "matcher": "*"}],
                    "preToolUse": [binding],
                }
            }
        )
    )
    project = tmp_path / "project"
    first = project / ".github/hooks/native-event-kit-events.json"
    personal = '{"version":1,"hooks":{"Stop":[{"command":"echo personal"}]}}'
    if collision:
        first.parent.mkdir(parents=True)
        first.write_text(personal)
    integrator = HookIntegrator()
    managed = {
        f".github/hooks/native-event-kit-{stem}.json"
        for stem in (("extra",) if collision else ("events", "extra"))
    }
    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"], package, project, managed_files=set()
    )
    before = {path: (project / path).read_bytes() for path in managed}
    bindings = [
        (event, entry["matcher"])
        for path in managed
        for event, entries in json.loads((project / path).read_text())["hooks"].items()
        for entry in entries
    ]
    assert sorted(bindings) == [
        ("PreToolUse", "*"),
        ("PreToolUse", "Bash"),
        ("preToolUse", "Bash"),
    ]
    integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["copilot"], package, project, managed_files=managed
    )
    assert before == {path: (project / path).read_bytes() for path in managed}
    if collision:
        assert first.read_text() == personal
