"""Configuration-led hook routing and Claude event rendering (#3226).

Hook filenames no longer choose destinations. Packages that relied on the
retired filename convention fail with migration guidance instead of silently
activating hooks the convention excluded or shadowed. Claude output only
contains events Claude Code accepts.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.skill_support import SkillIntegrationResult
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


class _NoopSkillIntegrator:
    def integrate_package_skill(self, package_info, project_root: Path, **kwargs):
        return SkillIntegrationResult(
            skill_created=False,
            skill_updated=False,
            skill_skipped=True,
            skill_path=None,
            references_copied=0,
        )


def _hook_only_target(name: str):
    target = KNOWN_TARGETS[name]
    return replace(target, primitives={"hooks": target.primitives["hooks"]})


def _command_hook(command: str, event: str = "Stop") -> dict[str, Any]:
    return {"hooks": {event: [{"type": "command", "command": command}]}}


def _package(tmp_path: Path, files: dict[str, dict[str, Any]], *, target=None) -> PackageInfo:
    package_root = tmp_path / "pkg"
    hooks_dir = package_root / "hooks"
    hooks_dir.mkdir(parents=True)
    for name, document in files.items():
        (hooks_dir / name).write_text(json.dumps(document), encoding="utf-8")
    package = APMPackage(name="hook-pkg", version="1.0.0", source="owner/hook-pkg")
    if target is not None:
        package.target = target
    return PackageInfo(package=package, install_path=package_root)


def _install(
    package_info: PackageInfo,
    project: Path,
    targets: list[str],
    *,
    dep_target_subset: list[str] | None = None,
) -> dict:
    project.mkdir(parents=True, exist_ok=True)
    return integrate_package_primitives(
        package_info,
        project,
        targets=[_hook_only_target(name) for name in targets],
        integrators=IntegratorBundle(
            prompt=None,
            agent=None,
            skill=_NoopSkillIntegrator(),
            instruction=None,
            command=None,
            hook=HookIntegrator(),
            canvas=None,
        ),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="hook-pkg",
        dep_target_subset=dep_target_subset,
    )


def _claude_hooks(project: Path) -> dict[str, list[dict[str, Any]]]:
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    return settings["hooks"]


def _commands(entries: list[dict[str, Any]]) -> list[str]:
    return [handler["command"] for entry in entries for handler in entry["hooks"]]


# -- configuration-led routing ------------------------------------------------


def test_generic_hook_file_reaches_every_effective_target(tmp_path: Path) -> None:
    package_info = _package(tmp_path, {"checks.json": _command_hook("echo checks")})
    project = tmp_path / "project"

    result = _install(package_info, project, ["claude", "copilot"])

    assert result["hooks"] == 2
    assert _commands(_claude_hooks(project)["Stop"]) == ["echo checks"]
    assert [path.name for path in (project / ".github" / "hooks").glob("*.json")] == [
        "pkg-checks.json"
    ]


def test_reporter_dot_suffix_name_is_an_ordinary_generic_file(tmp_path: Path) -> None:
    """``checks.claude.json`` never routed, so it stays a generic file."""
    package_info = _package(tmp_path, {"checks.claude.json": _command_hook("echo checks")})
    project = tmp_path / "project"

    result = _install(package_info, project, ["claude", "copilot"])

    assert result["hooks"] == 2
    assert (project / ".github" / "hooks" / "pkg-checks.claude.json").exists()


def test_package_target_declaration_limits_generic_hooks(tmp_path: Path) -> None:
    package_info = _package(
        tmp_path, {"checks.json": _command_hook("echo checks")}, target="claude"
    )
    project = tmp_path / "project"

    result = _install(package_info, project, ["claude", "copilot"])

    assert result["hooks"] == 1
    assert _commands(_claude_hooks(project)["Stop"]) == ["echo checks"]
    assert not (project / ".github" / "hooks").exists()


def test_legacy_name_without_hidden_files_installs_for_its_configured_target(
    tmp_path: Path,
) -> None:
    package_info = _package(tmp_path, {"my-pkg-claude-hooks.json": _command_hook("echo claude")})
    project = tmp_path / "project"

    result = _install(package_info, project, ["claude", "copilot"], dep_target_subset=["claude"])

    assert result["hooks"] == 1
    assert _commands(_claude_hooks(project)["Stop"]) == ["echo claude"]


def test_target_word_in_filename_no_longer_alters_destination(tmp_path: Path) -> None:
    """A Copilot-named file follows configuration, not its name."""
    package_info = _package(
        tmp_path,
        {"my-pkg-copilot-hooks.json": _command_hook("echo named")},
        target="claude",
    )
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)

    with pytest.raises(HookContractError, match=r"Target 'claude' would newly activate"):
        _install(package_info, project, ["claude", "copilot"])


@pytest.mark.parametrize(
    ("files", "targets", "widened"),
    [
        pytest.param(
            {
                "hooks.json": _command_hook("echo generic"),
                "x-claude-hooks.json": _command_hook("c"),
            },
            ["claude"],
            {"claude": "hooks/hooks.json"},
            id="shadowed-generic-hook",
        ),
        pytest.param(
            {"x-claude-hooks.json": _command_hook("echo claude")},
            ["copilot"],
            {"copilot": "hooks/x-claude-hooks.json"},
            id="other-target-script",
        ),
        pytest.param(
            {
                "hooks.json": _command_hook("echo generic"),
                "hooks-copilot.json": _command_hook("echo copilot"),
            },
            ["claude", "copilot"],
            {"claude": "hooks/hooks-copilot.json", "copilot": "hooks/hooks.json"},
            id="split-per-target-files",
        ),
    ],
)
def test_legacy_reliance_fails_before_any_hook_write(
    tmp_path: Path,
    files: dict[str, dict[str, Any]],
    targets: list[str],
    widened: dict[str, str],
) -> None:
    package_info = _package(tmp_path, files)
    project = tmp_path / "project"
    claude_settings = project / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    known_good = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
    claude_settings.write_text(json.dumps(known_good), encoding="utf-8")

    with pytest.raises(HookContractError) as excinfo:
        _install(package_info, project, targets)

    message = str(excinfo.value)
    assert "owner/hook-pkg: hook filename target routing was removed" in message
    for target_name, path in widened.items():
        assert f"Target '{target_name}' would newly activate: " in message
        assert path in message
    assert "target: <name>" in message
    assert "targets: [...]" in message
    assert json.loads(claude_settings.read_text(encoding="utf-8")) == known_good
    assert not (project / ".github" / "hooks").exists()
    assert not (project / ".claude" / "hooks").exists()


def test_direct_target_integration_refuses_legacy_widening(tmp_path: Path) -> None:
    package_info = _package(
        tmp_path,
        {"hooks.json": _command_hook("echo generic"), "x-claude-hooks.json": _command_hook("c")},
    )
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(HookContractError, match=r"newly activate: hooks/hooks\.json"):
        HookIntegrator().integrate_package_hooks_claude(package_info, project)
    assert not (project / ".claude" / "settings.json").exists()


# -- Claude native events -----------------------------------------------------


def test_copilot_events_render_as_claude_events_without_duplicates(tmp_path: Path) -> None:
    """The reported manifest: both spellings in one file, installed for Claude."""
    hooks = {
        "SubagentStop": [{"matcher": "*", "hooks": [{"type": "command", "command": "ledger"}]}],
        "subagentStop": [{"type": "command", "command": "ledger"}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}],
        "preToolUse": [{"type": "command", "command": "guard"}],
    }
    package_info = _package(tmp_path, {"hooks.json": {"hooks": hooks}})
    project = tmp_path / "project"

    for _ in range(2):
        _install(package_info, project, ["claude"])

    claude = _claude_hooks(project)
    assert set(claude) == {"SubagentStop", "PreToolUse"}
    assert len(claude["SubagentStop"]) == 1
    assert sorted(entry["matcher"] for entry in claude["PreToolUse"]) == ["*", "Bash"]


@pytest.mark.parametrize(
    ("source_event", "claude_event"),
    [
        ("subagentStop", "SubagentStop"),
        ("sessionEnd", "SessionEnd"),
        ("userPromptSubmitted", "UserPromptSubmit"),
        ("userPromptSubmit", "UserPromptSubmit"),
        ("postToolUseFailure", "PostToolUseFailure"),
        ("preCompact", "PreCompact"),
    ],
)
def test_documented_copilot_aliases_render_claude_events(
    tmp_path: Path, source_event: str, claude_event: str
) -> None:
    package_info = _package(tmp_path, {"hooks.json": _command_hook("echo x", source_event)})
    project = tmp_path / "project"

    _install(package_info, project, ["claude"])

    assert set(_claude_hooks(project)) == {claude_event}


@pytest.mark.parametrize("event", ["errorOccurred", "subagentStart", "stop"])
def test_event_without_claude_equivalent_fails_before_settings_change(
    tmp_path: Path, event: str
) -> None:
    package_info = _package(
        tmp_path,
        {"hooks.json": {"hooks": {"Stop": [{"type": "command", "command": "ok"}], event: []}}},
    )
    project = tmp_path / "project"
    claude_settings = project / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text('{"model": "keep"}', encoding="utf-8")

    with pytest.raises(
        HookContractError, match=rf"no documented Claude Code name \(hooks.json: {event}\)"
    ):
        _install(package_info, project, ["claude"])

    assert claude_settings.read_text(encoding="utf-8") == '{"model": "keep"}'


def test_copilot_only_event_still_installs_for_copilot(tmp_path: Path) -> None:
    package_info = _package(tmp_path, {"hooks.json": _command_hook("echo err", "errorOccurred")})
    project = tmp_path / "project"

    result = _install(package_info, project, ["claude", "copilot"], dep_target_subset=["copilot"])

    assert result["hooks"] == 1
    deployed = json.loads(
        (project / ".github" / "hooks" / "pkg-hooks.json").read_text(encoding="utf-8")
    )
    assert set(deployed["hooks"]) == {"errorOccurred"}
    assert not (project / ".claude").exists()
