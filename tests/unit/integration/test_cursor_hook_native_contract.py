"""Cursor native output and third-party import regression contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.hook_integrator import HookIntegrator, native_hook_config
from apm_cli.integration.hook_native_formats import inspect_native_hooks
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _package(tmp_path: Path, hooks: dict[str, Any]) -> PackageInfo:
    root = tmp_path / "cursor-gate"
    _write_json(root / ".apm/hooks/gate.json", {"hooks": hooks})
    script = root / ".apm/hooks/scripts/gate.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('{}')\n", encoding="utf-8")
    return PackageInfo(
        package=APMPackage(name="cursor-gate", version="1.0.0"),
        install_path=root,
    )


def _nested(command: str, **group: Any) -> dict[str, Any]:
    return {**group, "hooks": [{"type": "command", "command": command, "timeout": 10}]}


def test_cursor_install_emits_native_events_and_flat_handlers(tmp_path: Path) -> None:
    """Pin native JSON independently of the production event/handler tables."""
    command = 'python "${PLUGIN_ROOT}/.apm/hooks/scripts/gate.py"'
    package = _package(
        tmp_path,
        {
            "PreToolUse": [_nested(command, matcher="Bash")],
            "PostToolUse": [_nested(command, matcher="Edit|Write")],
            "UserPromptSubmit": [_nested(command)],
            "PreCompact": [_nested(command)],
            "Stop": [_nested(command)],
        },
    )
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    result = HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    native = {
        "type": "command",
        "command": 'python ".cursor/hooks/cursor-gate/.apm/hooks/scripts/gate.py"',
        "timeout": 10,
    }
    expected = {
        "version": 1,
        "hooks": {
            "preToolUse": [{**native, "matcher": "Shell"}],
            "postToolUse": [{**native, "matcher": "Write"}],
            "beforeSubmitPrompt": [native],
            "preCompact": [native],
            "stop": [{**native, "loop_limit": None}],
        },
    }
    assert json.loads((project / ".cursor/hooks.json").read_text()) == expected
    assert result.files_integrated == 1
    assert (project / ".cursor/hooks/cursor-gate/.apm/hooks/scripts/gate.py").is_file()
    assert json.loads(result.display_payloads[0]["rendered_json"])["hooks"] == expected["hooks"]


def test_cursor_refusal_precedes_all_hook_bundle_writes(tmp_path: Path) -> None:
    package = _package(tmp_path, {"preToolUse": [{"command": "echo ok"}]})
    hooks = package.install_path / ".apm/hooks"
    _write_json(
        hooks / "a-valid.json",
        {"hooks": {"preToolUse": [_nested('python "${PLUGIN_ROOT}/.apm/hooks/scripts/gate.py"')]}},
    )
    _write_json(hooks / "z-unsupported.json", {"hooks": {"Notification": [_nested("echo bad")]}})
    project = tmp_path / "project"
    user = {"version": 1, "hooks": {"afterFileEdit": [{"command": "echo user"}]}}
    path = project / ".cursor/hooks.json"
    _write_json(path, user)
    before = path.read_bytes()

    with pytest.raises(HookContractError, match="unsupported Cursor event"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert path.read_bytes() == before
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()) == [
        ".cursor/hooks.json"
    ]


@pytest.mark.parametrize("first", ["cursor", "claude"])
def test_import_overlap_refused_in_either_install_order(tmp_path: Path, first: str) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared", matcher="Bash")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    integrator = HookIntegrator()
    integrator.integrate_hooks_for_target(KNOWN_TARGETS[first], package, project)
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    second = "claude" if first == "cursor" else "cursor"

    with pytest.raises(HookContractError, match="Claude import"):
        integrator.integrate_hooks_for_target(KNOWN_TARGETS[second], package, project)

    after = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert after == before


def test_planned_overlap_refused_before_either_target_is_written(tmp_path: Path) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared", matcher="Bash")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    targets = [KNOWN_TARGETS[name] for name in ("claude", "cursor")]
    plan = DeployableSourcePlan.create(
        package,
        targets,
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )

    with pytest.raises(HookContractError, match="Claude import"):
        HookIntegrator().integrate_hooks_for_target(targets[0], package, project, source_plan=plan)

    assert not list(project.rglob("*.json"))


@pytest.mark.parametrize(
    ("source", "native"),
    [
        ("PreToolUse", "preToolUse"),
        ("PostToolUse", "postToolUse"),
        ("UserPromptSubmit", "beforeSubmitPrompt"),
        ("Stop", "stop"),
        ("SubagentStop", "subagentStop"),
        ("SessionStart", "sessionStart"),
        ("SessionEnd", "sessionEnd"),
        ("PreCompact", "preCompact"),
    ],
)
def test_documented_cursor_import_event_mappings(tmp_path: Path, source: str, native: str) -> None:
    package = _package(tmp_path, {source: [_nested("echo check")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    data = json.loads((project / ".cursor/hooks.json").read_text())
    assert list(data["hooks"]) == [native]
    entry = data["hooks"][native][0]
    assert entry["command"] == "echo check"
    assert "hooks" not in entry
    if native in ("stop", "subagentStop"):
        assert entry["loop_limit"] is None


@pytest.mark.parametrize(
    ("event", "entry", "message"),
    [
        ("Notification", _nested("echo check"), "unsupported Cursor event"),
        ("PermissionRequest", _nested("echo check"), "unsupported Cursor event"),
        ("userPromptSubmitted", _nested("echo check"), "unsupported Cursor event"),
        ("agentStop", _nested("echo check"), "unsupported Cursor event"),
        ("PreToolUse", _nested("echo check", matcher="mcp__ide__.*"), "cannot preserve"),
        ("PreToolUse", _nested("echo check", matcher="Glob|Bash"), "cannot preserve"),
        ("PreToolUse", _nested("echo check", matcher="^Bash$"), "cannot preserve"),
        ("PreToolUse", _nested("echo check", matcher="Write"), "both Claude Edit and Write"),
        ("PreToolUse", _nested("echo check", matcher="Edit"), "both Claude Edit and Write"),
        ("SessionStart", _nested("echo check", matcher="startup"), "no verified Cursor equivalent"),
        ("preToolUse", {"bash": "echo check"}, "platform-specific"),
        (
            "preToolUse",
            {"command": "echo check", "linux": "echo posix"},
            "unsupported Cursor handler",
        ),
        ("preToolUse", {"command": "echo check", "async": True}, "unsupported Cursor handler"),
        ("preToolUse", {"type": "agent", "prompt": "verify"}, "only command and prompt"),
        ("preToolUse", {"type": {}, "command": "echo check"}, "only command and prompt"),
        ("preToolUse", {"command": "echo check", "matcher": None}, "matcher must be a string"),
        ("preToolUse", {"command": "echo check", "timeout": None}, "finite positive"),
        ("preToolUse", {"command": "echo check", "timeout": True}, "finite positive"),
        ("preToolUse", {"command": "echo check", "timeout": float("inf")}, "finite positive"),
        (
            "preToolUse",
            {"command": "echo check", "timeout": 2, "timeoutSec": 3},
            "only one timeout",
        ),
        ("preToolUse", {"command": "echo check", "failClosed": "true"}, "must be a boolean"),
    ],
)
def test_unrepresentable_hooks_fail_without_native_or_script_writes(
    tmp_path: Path, event: str, entry: dict[str, Any], message: str
) -> None:
    package = _package(tmp_path, {event: [entry]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    with pytest.raises(HookContractError, match=message):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert list((project / ".cursor").iterdir()) == []


def test_native_prompt_matcher_and_restrictions_are_preserved(tmp_path: Path) -> None:
    native = {
        "type": "prompt",
        "prompt": "Allow only safe $ARGUMENTS",
        "model": "test-model",
        "timeout": 10,
        "failClosed": True,
        "matcher": "curl|wget",
    }
    package = _package(tmp_path, {"beforeShellExecution": [native]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert json.loads((project / ".cursor/hooks.json").read_text()) == {
        "version": 1,
        "hooks": {"beforeShellExecution": [native]},
    }
    contract = native_hook_config("cursor")
    assert contract is not None
    inspected = inspect_native_hooks(
        json.loads((project / ".cursor/hooks.json").read_text()),
        "cursor_hooks",
        prompt_types=contract.prompt_handler_types,
        nested_handlers=contract.nested_handlers,
    )
    assert [entry.prompt for entry in inspected] == [native["prompt"]]
    assert all(entry.error is None for entry in inspected)


def test_cursor_install_reinstall_and_remove_preserve_user_hooks(tmp_path: Path) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo installed")]})
    project = tmp_path / "project"
    path = project / ".cursor/hooks.json"
    user = {"version": 1, "hooks": {"afterFileEdit": [{"command": "echo user"}]}}
    _write_json(path, user)
    integrator = HookIntegrator()
    target = KNOWN_TARGETS["cursor"]
    integrator.integrate_hooks_for_target(target, package, project)
    first = path.read_bytes()

    integrator.integrate_hooks_for_target(target, package, project)

    assert path.read_bytes() == first
    stats = integrator.reconcile_package_target_restriction(package, project, [target])
    assert stats["errors"] == 0
    assert json.loads(path.read_text()) == user
    assert not path.with_name("apm-hooks.json").exists()


def test_reinstall_migrates_only_owned_legacy_events(tmp_path: Path) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo installed")]})
    project = tmp_path / "project"
    legacy = _nested("echo installed")
    _write_json(
        project / ".cursor/hooks.json",
        {
            "version": 1,
            "hooks": {"PreToolUse": [legacy], "afterFileEdit": [{"command": "echo user"}]},
        },
    )
    _write_json(
        project / ".cursor/apm-hooks.json",
        {
            "PreToolUse": [{**legacy, "_apm_source": "cursor-gate"}],
        },
    )

    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    data = json.loads((project / ".cursor/hooks.json").read_text())
    assert data["hooks"] == {
        "afterFileEdit": [{"command": "echo user"}],
        "preToolUse": [{"type": "command", "command": "echo installed", "timeout": 10}],
    }
    assert set(json.loads((project / ".cursor/apm-hooks.json").read_text())) == {"preToolUse"}


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        "[]",
        '{"version":2,"hooks":{}}',
        '{"version":1,"hooks":{"PreToolUse":[{"command":"echo user"}]}}',
    ],
)
def test_invalid_user_config_is_not_replaced(tmp_path: Path, payload: str) -> None:
    package = _package(tmp_path, {"preToolUse": [{"command": "echo installed"}]})
    project = tmp_path / "project"
    path = project / ".cursor/hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload)

    with pytest.raises(HookContractError):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert path.read_text() == payload
    assert not path.with_name("apm-hooks.json").exists()


@pytest.mark.parametrize("location", ["project", "local", "user"])
def test_import_locations_preserved_and_overlap_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project, home = tmp_path / "project", tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (project / ".cursor").mkdir(parents=True)
    paths = {
        "project": project / ".claude/settings.json",
        "local": project / ".claude/settings.local.json",
        "user": home / ".claude/settings.json",
    }
    _write_json(paths[location], {"hooks": {"PreToolUse": [_nested("echo shared")]}})
    before = paths[location].read_bytes()

    with pytest.raises(HookContractError, match="Claude import"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert paths[location].read_bytes() == before
    assert not (project / ".cursor/hooks.json").exists()


def test_unrelated_claude_user_hooks_do_not_block_cursor(tmp_path: Path) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo installed")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    claude = project / ".claude/settings.json"
    _write_json(claude, {"hooks": {"PreToolUse": [_nested("echo unrelated")]}})
    before = claude.read_bytes()

    result = HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert result.files_integrated == 1
    assert claude.read_bytes() == before


@pytest.mark.parametrize("names", [("claude", "cursor"), ("cursor", "claude")])
@pytest.mark.parametrize("cursor_exists", [False, True])
def test_install_service_preflight_prevents_partial_multi_target_deployment(
    tmp_path: Path, names: tuple[str, str], cursor_exists: bool
) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project = tmp_path / "project"
    project.mkdir()
    if cursor_exists:
        (project / ".cursor").mkdir()
    sibling = MagicMock()
    sibling.preflight_instructions_for_targets.side_effect = AssertionError(
        "Hook preflight must reject before reaching sibling primitives"
    )
    bundle = IntegratorBundle(
        prompt=sibling,
        agent=sibling,
        skill=sibling,
        instruction=sibling,
        command=sibling,
        hook=HookIntegrator(),
    )

    with pytest.raises(HookContractError, match="Claude import"):
        integrate_package_primitives(
            package,
            project,
            targets=[KNOWN_TARGETS[name] for name in names],
            integrators=bundle,
            diagnostics=DiagnosticCollector(),
            force=False,
            managed_files=None,
        )

    assert not list(project.rglob("*.json"))
    sibling.integrate_package_skill.assert_not_called()
    sibling.preflight_instructions_for_targets.assert_not_called()


def test_unapproved_hooks_do_not_enter_cursor_preflight(tmp_path: Path) -> None:
    package = _package(tmp_path, {"Notification": [_nested("echo never")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    hook = MagicMock()
    target = KNOWN_TARGETS["cursor"]
    bundle = IntegratorBundle(
        prompt=None, agent=None, skill=SkillIntegrator(), instruction=None, command=None, hook=hook
    )

    integrate_package_primitives(
        package,
        project,
        targets=[replace(target, primitives={"hooks": target.primitives["hooks"]})],
        integrators=bundle,
        diagnostics=DiagnosticCollector(),
        allow_executables={},
        force=False,
        managed_files=None,
    )

    hook.preflight_hooks_for_targets.assert_not_called()
    hook.integrate_hooks_for_target.assert_not_called()
    assert not list(project.rglob("*.json"))


@pytest.mark.parametrize("first", ["claude", "cursor"])
def test_explicit_target_contraction_can_choose_one_import_route(
    tmp_path: Path, first: str
) -> None:
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    hook = HookIntegrator()
    hook.integrate_hooks_for_target(KNOWN_TARGETS[first], package, project)
    second = "cursor" if first == "claude" else "claude"
    package.package = APMPackage(name="cursor-gate", version="1.0.0", targets=[second])
    targets = [
        replace(target, primitives={"hooks": target.primitives["hooks"]})
        for target in (KNOWN_TARGETS["claude"], KNOWN_TARGETS["cursor"])
    ]

    integrate_package_primitives(
        package,
        project,
        targets=targets,
        force=False,
        managed_files=None,
        integrators=IntegratorBundle(
            prompt=None,
            agent=None,
            skill=SkillIntegrator(),
            instruction=None,
            command=None,
            hook=hook,
        ),
        diagnostics=DiagnosticCollector(),
    )

    first_path = project / f".{first}" / ("settings.json" if first == "claude" else "hooks.json")
    second_path = project / f".{second}" / ("settings.json" if second == "claude" else "hooks.json")
    assert not json.loads(first_path.read_text()).get("hooks")
    assert json.loads(second_path.read_text())["hooks"]
    assert not first_path.with_name("apm-hooks.json").exists()


@pytest.mark.parametrize("field", ["disableAllHooks", "enabled"])
def test_cursor_does_not_discard_source_level_settings(tmp_path: Path, field: str) -> None:
    package = _package(tmp_path, {"preToolUse": [{"command": "echo gated"}]})
    _write_json(
        package.install_path / ".apm/hooks/gate.json",
        {field: False, "hooks": {"preToolUse": [{"command": "echo gated"}]}},
    )
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    with pytest.raises(HookContractError, match="unsupported Cursor source fields"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)
    assert not list(project.rglob("*.json"))


@pytest.mark.parametrize("owner", [None, [], {}])
def test_malformed_inline_ownership_is_not_a_cleanup_grant(tmp_path: Path, owner: Any) -> None:
    package = _package(tmp_path, {"preToolUse": [{"command": "echo package"}]})
    project = tmp_path / "project"
    path = project / ".cursor/hooks.json"
    _write_json(
        path,
        {
            "version": 1,
            "hooks": {"preToolUse": [{"command": "echo user", "_apm_source": owner}]},
        },
    )
    before = path.read_bytes()
    with pytest.raises(HookContractError, match="invalid Cursor hook ownership"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)
    assert path.read_bytes() == before


@pytest.mark.parametrize("first", ["claude", "cursor"])
@pytest.mark.parametrize("global_owner", [False, True], ids=["project-user", "global-owned"])
def test_retirement_does_not_ignore_user_or_global_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    global_owner: bool,
) -> None:
    project, home = tmp_path / "project", tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (project / ".cursor").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    package = _package(tmp_path, {"PreToolUse": [_nested("echo original")]})
    hook = HookIntegrator()
    hook.integrate_hooks_for_target(KNOWN_TARGETS[first], package, project)
    if global_owner:
        hook.integrate_hooks_for_target(KNOWN_TARGETS[first], package, home, user_scope=True)
    else:
        path = project / f".{first}" / ("settings.json" if first == "claude" else "hooks.json")
        document = json.loads(path.read_text())
        event = "PreToolUse" if first == "claude" else "preToolUse"
        entry = (
            _nested("echo user-overlap") if first == "claude" else {"command": "echo user-overlap"}
        )
        document["hooks"][event].append(entry)
        _write_json(path, document)
        _write_json(
            package.install_path / ".apm/hooks/gate.json",
            {"hooks": {"PreToolUse": [_nested("echo user-overlap")]}},
        )
    second = "cursor" if first == "claude" else "claude"
    package.package = APMPackage(name="cursor-gate", version="1.0.0", targets=[second])
    before = {p: p.read_bytes() for root in (project, home) for p in root.rglob("*") if p.is_file()}
    with pytest.raises(HookContractError, match="Claude import"):
        integrate_package_primitives(
            package,
            project,
            targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["cursor"]],
            force=False,
            managed_files=None,
            integrators=IntegratorBundle(
                prompt=None,
                agent=None,
                skill=SkillIntegrator(),
                instruction=None,
                command=None,
                hook=hook,
            ),
            diagnostics=DiagnosticCollector(),
        )
    assert {
        p: p.read_bytes() for root in (project, home) for p in root.rglob("*") if p.is_file()
    } == before


# --- Reused-plan preflight regression (#3129) ------------------------------
#
# The up-front `preflight_hooks_for_targets` call and the per-target write
# boundary inside `_integrate_merged_hooks` used to share a single
# `DeployableSourcePlan.cursor_preflight_done` cache bit: once set True by
# one call, later per-target writes for the SAME plan object skipped the
# re-check entirely. A plan is not tied to one write: the same plan can be
# reused for a second `integrate_hooks_for_target` call in the same project
# after a Claude import file appears, or reused verbatim against a second
# project that already has one. Both cases must still be rejected.


def test_reused_plan_rejects_claude_import_added_after_upfront_preflight(tmp_path: Path) -> None:
    """A Claude import appearing after the up-front preflight must still be caught."""
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["cursor"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    integrator = HookIntegrator()

    # Up-front preflight runs against a clean project: nothing to reject yet.
    integrator.preflight_hooks_for_targets(package, project, plan)

    # A Claude import with the same shared action appears after that check,
    # before the per-target write actually happens.
    _write_json(
        project / ".claude/settings.json",
        {"hooks": {"PreToolUse": [_nested("echo shared")]}},
    )

    with pytest.raises(HookContractError, match="Claude import"):
        integrator.integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"], package, project, source_plan=plan
        )

    assert not (project / ".cursor/hooks.json").exists()


def test_reused_plan_rejects_overlap_in_a_different_project(tmp_path: Path) -> None:
    """The same plan object reused for a second project must still be rejected."""
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    (project_a / ".cursor").mkdir(parents=True)
    (project_b / ".cursor").mkdir(parents=True)
    _write_json(
        project_b / ".claude/settings.json",
        {"hooks": {"PreToolUse": [_nested("echo shared")]}},
    )
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["cursor"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    integrator = HookIntegrator()

    # First project is clean; the plan's up-front preflight and the write
    # both succeed and must not poison later use of the same plan object.
    integrator.preflight_hooks_for_targets(package, project_a, plan)
    result = integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["cursor"], package, project_a, source_plan=plan
    )
    assert result.files_integrated == 1

    # Reusing the SAME plan for project_b, which already has an overlapping
    # Claude import, must still be rejected -- not silently bypassed because
    # the plan was already "preflighted" for project_a.
    with pytest.raises(HookContractError, match="Claude import"):
        integrator.integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"], package, project_b, source_plan=plan
        )

    assert not (project_b / ".cursor/hooks.json").exists()


def test_reused_plan_rejects_overlap_in_a_different_project_before_any_write(
    tmp_path: Path,
) -> None:
    """Switching projects BEFORE the plan's first native write must still reject.

    The sibling case above performs a real write for project_a before
    switching to project_b, proving reuse-after-use is caught. This case
    proves the narrower, earlier scenario: the plan only ran its up-front
    ``preflight_hooks_for_targets`` for project_a -- no native write ever
    happened there -- before being reused for project_b's own overlapping
    import. Both project roots must stay exactly as they started.
    """
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    (project_a / ".cursor").mkdir(parents=True)
    (project_b / ".cursor").mkdir(parents=True)
    _write_json(
        project_b / ".claude/settings.json",
        {"hooks": {"PreToolUse": [_nested("echo shared")]}},
    )
    before_b = (project_b / ".claude/settings.json").read_bytes()
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["cursor"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    integrator = HookIntegrator()

    # Up-front preflight only, against the clean project_a. No native write
    # for project_a happens anywhere in this test.
    integrator.preflight_hooks_for_targets(package, project_a, plan)

    # The same plan object -- "preflighted" only against project_a, never
    # written anywhere yet -- is reused directly against project_b, which
    # already has an overlapping Claude import.
    with pytest.raises(HookContractError, match="Claude import"):
        integrator.integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"], package, project_b, source_plan=plan
        )

    assert not (project_a / ".cursor/hooks.json").exists()
    assert not (project_b / ".cursor/hooks.json").exists()
    assert (project_b / ".claude/settings.json").read_bytes() == before_b


def test_reused_plan_still_succeeds_without_a_conflicting_import(tmp_path: Path) -> None:
    """Control: the unconditional re-check must not over-reject the clean case."""
    package = _package(tmp_path, {"PreToolUse": [_nested("echo shared")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["cursor"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    integrator = HookIntegrator()

    integrator.preflight_hooks_for_targets(package, project, plan)
    result = integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["cursor"], package, project, source_plan=plan
    )

    assert result.files_integrated == 1
    assert (project / ".cursor/hooks.json").exists()


def test_reused_plan_preserves_unrelated_user_owned_claude_hooks(tmp_path: Path) -> None:
    """The re-check must not flag or touch an unrelated, non-overlapping user hook."""
    package = _package(tmp_path, {"PreToolUse": [_nested("echo installed")]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    claude = project / ".claude/settings.json"
    _write_json(claude, {"hooks": {"PreToolUse": [_nested("echo unrelated-user-hook")]}})
    before = claude.read_bytes()
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["cursor"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
    )
    integrator = HookIntegrator()

    integrator.preflight_hooks_for_targets(package, project, plan)
    result = integrator.integrate_hooks_for_target(
        KNOWN_TARGETS["cursor"], package, project, source_plan=plan
    )

    assert result.files_integrated == 1
    assert claude.read_bytes() == before
