"""User-scope hook commands anchor to home only where the harness shell expands it (#2821)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.integration import targets as targets_mod
from apm_cli.integration.hook_command_paths import splice_references
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, UserHookHomeReference
from apm_cli.models.apm_package import APMPackage, PackageInfo

_VERIFIED = {
    "claude": ("command", "bash"),
    "copilot": ("bash",),
    "gemini": ("command",),
    "kiro": ("command", "bash"),
}
_UNVERIFIED_WITH_USER_HOOKS = ("codex", "cursor", "windsurf")
_SCRIPT = "run.sh"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a fake POSIX home that is also the user-scope deploy root."""
    root = tmp_path / "home-a"
    root.mkdir()
    _use_home(monkeypatch, root)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: True)
    return root


def _use_home(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))


def _package(tmp_path: Path, document: dict, name: str = "portable-pkg") -> PackageInfo:
    package_dir = tmp_path / "packages" / name
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")
    (hooks_dir / _SCRIPT).write_text("#!/bin/sh\necho portable\n", encoding="utf-8")
    return PackageInfo(package=APMPackage(name=name, version="1.0.0"), install_path=package_dir)


def _nested(handler: dict, event: str = "PreToolUse") -> dict:
    return {"hooks": {event: [{"matcher": "*", "hooks": [{"type": "command", **handler}]}]}}


def _flat(handler: dict, event: str = "preToolUse") -> dict:
    return {"version": 1, "hooks": {event: [{"type": "command", **handler}]}}


def _install(target_name: str, package: PackageInfo, home: Path, profile=None) -> None:
    profile = profile or KNOWN_TARGETS[target_name].for_scope(user_scope=True)
    root = home / profile.root_dir
    root.mkdir(parents=True, exist_ok=True)
    result = HookIntegrator().integrate_hooks_for_target(profile, package, home, user_scope=True)
    assert result.hooks_integrated >= 1


def _commands(home: Path, target_name: str) -> list[str]:
    root = home / KNOWN_TARGETS[target_name].for_scope(user_scope=True).root_dir
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and _SCRIPT in value:
            found.append(value)

    for path in sorted(root.rglob("*.json")):
        if path.name != "apm-hooks.json":
            walk(json.loads(path.read_text(encoding="utf-8")))
    return found


def _rewrite(home: Path, command: str, reference=None, **kwargs) -> str:
    package = home.parent / "pkg"
    script = package / "hooks" / _SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    rewritten, _ = HookIntegrator()._rewrite_command_for_target(
        command,
        package,
        "my-pkg",
        "claude",
        deploy_root=kwargs.pop("deploy_root", home),
        home_reference=reference or KNOWN_TARGETS["claude"].user_hook_home_reference,
        **kwargs,
    )
    return rewritten


class TestCapabilityCatalog:
    def test_only_documented_shell_harnesses_enable_home_reference(self) -> None:
        enabled = {
            name: profile.user_hook_home_reference.shell_selectors
            for name, profile in KNOWN_TARGETS.items()
            if profile.user_hook_home_reference is not None
        }
        assert enabled == _VERIFIED
        for name in _UNVERIFIED_WITH_USER_HOOKS:
            assert KNOWN_TARGETS[name].supports_at_user_scope("hooks")
            assert KNOWN_TARGETS[name].user_hook_home_reference is None

    @pytest.mark.parametrize("name", sorted(_VERIFIED))
    def test_enabled_capability_cites_official_docs(self, name: str) -> None:
        reference = KNOWN_TARGETS[name].user_hook_home_reference
        assert reference is not None
        assert reference.reference == "$HOME"
        assert urlparse(reference.evidence).scheme == "https"
        assert KNOWN_TARGETS[name].for_scope(user_scope=True).user_hook_home_reference is reference

    def test_reference_requires_shell_field_quote_and_posix_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reference = UserHookHomeReference(shell_selectors=("bash",), evidence="https://x.test")
        monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: True)
        assert reference.reference_for("bash", quote='"') == "$HOME"
        assert reference.reference_for("bash", quote="") == "$HOME"
        assert reference.reference_for("bash", quote="'") is None
        assert reference.reference_for("powershell", quote="") is None
        assert reference.for_handler({"command": "./run.sh"}) is reference
        assert reference.for_handler({"command": "./run.sh", "args": []}) is None
        monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: False)
        assert reference.reference_for("bash", quote="") is None


class TestRewrite:
    def test_plugin_root_and_relative_references_anchor_to_home(self, home: Path) -> None:
        assert _rewrite(home, "sh ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh") == (
            "sh $HOME/.claude/hooks/my-pkg/hooks/run.sh"
        )
        assert _rewrite(home, 'sh "./run.sh"', hook_file_dir=home.parent / "pkg" / "hooks") == (
            'sh "$HOME/.claude/hooks/my-pkg/run.sh"'
        )

    def test_repeated_reference_keeps_each_quote_policy(self, home: Path) -> None:
        absolute = (home / ".claude/hooks/my-pkg/hooks/run.sh").resolve()
        assert (
            _rewrite(
                home,
                "sh \"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh\" && sh '${CLAUDE_PLUGIN_ROOT}/hooks/run.sh'",
            )
            == f"sh \"$HOME/.claude/hooks/my-pkg/hooks/run.sh\" && sh '{absolute}'"
        )
        hook_dir = home.parent / "pkg" / "hooks"
        assert _rewrite(home, "sh ./run.sh && sh './run.sh'", hook_file_dir=hook_dir) == (
            f"sh $HOME/.claude/hooks/my-pkg/run.sh && sh '{home / '.claude/hooks/my-pkg/run.sh'}'"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"source_key": "powershell"},
            {"reference": UserHookHomeReference(("bash",), "https://x.test")},
        ],
        ids=["powershell-shell", "field-not-shell-executed"],
    )
    def test_non_shell_forms_stay_absolute(self, home: Path, kwargs: dict) -> None:
        absolute = (home / ".claude/hooks/my-pkg/hooks/run.sh").resolve()
        assert _rewrite(home, "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh", **kwargs) == str(absolute)

    def test_windows_host_stays_absolute(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: False)
        absolute = (home / ".claude/hooks/my-pkg/hooks/run.sh").resolve()
        assert _rewrite(home, "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh") == str(absolute)

    def test_custom_root_outside_home_stays_absolute(self, home: Path) -> None:
        custom = home.parent / "claude-config"
        assert _rewrite(home, "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh", root_dir=str(custom)) == str(
            custom / "hooks/my-pkg/hooks/run.sh"
        )

    def test_deploy_root_other_than_home_stays_absolute(self, home: Path) -> None:
        other = home.parent / "other"
        assert _rewrite(home, "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh", deploy_root=other) == str(
            (other / ".claude/hooks/my-pkg/hooks/run.sh").resolve()
        )


class TestUserScopeInstall:
    @pytest.mark.parametrize(
        ("target_name", "document", "expected"),
        [
            ("claude", _nested({"command": "sh ./run.sh"}), ".claude/hooks/portable-pkg"),
            ("gemini", _nested({"command": "sh ./run.sh"}), ".gemini/hooks/portable-pkg"),
            ("kiro", _nested({"command": "sh ./run.sh"}), ".kiro/hooks/portable-pkg"),
            ("copilot", _flat({"bash": "sh ./run.sh"}), ".copilot/hooks/scripts/portable-pkg"),
        ],
    )
    def test_verified_harness_writes_home_reference(
        self, home: Path, tmp_path: Path, target_name: str, document: dict, expected: str
    ) -> None:
        _install(target_name, _package(tmp_path, document), home)
        assert _commands(home, target_name) == [f"sh $HOME/{expected}/{_SCRIPT}"]
        assert (home / expected / _SCRIPT).is_file()

    @pytest.mark.parametrize(
        ("target_name", "document"),
        [
            ("claude", _nested({"command": "./run.sh", "args": ["--check"]})),
            ("claude", _nested({"command": "sh ./run.sh", "shell": "powershell"})),
            ("copilot", _flat({"command": "sh ./run.sh"})),
            ("codex", _nested({"command": "sh ./run.sh"})),
            ("cursor", _flat({"command": "sh ./run.sh"}, event="preToolUse")),
            ("windsurf", _flat({"command": "sh ./run.sh"}, event="pre_run_command")),
        ],
        ids=[
            "claude-exec-form",
            "claude-powershell",
            "copilot-cross-platform-command",
            "codex-unverified",
            "cursor-unverified",
            "windsurf-unverified",
        ],
    )
    def test_unverified_or_non_shell_handlers_keep_absolute_paths(
        self, home: Path, tmp_path: Path, target_name: str, document: dict
    ) -> None:
        _install(target_name, _package(tmp_path, document), home)
        commands = _commands(home, target_name)
        assert commands
        assert all("$HOME" not in command for command in commands)
        assert all(str(home.resolve()) in command for command in commands)

    def test_reinstall_over_absolute_install_migrates_in_place(
        self, home: Path, tmp_path: Path
    ) -> None:
        package = _package(tmp_path, _nested({"command": "sh ./run.sh"}))
        legacy = replace(
            KNOWN_TARGETS["claude"].for_scope(user_scope=True), user_hook_home_reference=None
        )
        _install("claude", package, home, profile=legacy)
        assert _commands(home, "claude") == [
            f"sh {(home / '.claude/hooks/portable-pkg' / _SCRIPT).resolve()}"
        ]

        _install("claude", package, home)

        assert _commands(home, "claude") == [f"sh $HOME/.claude/hooks/portable-pkg/{_SCRIPT}"]
        sidecar = json.loads((home / ".claude" / "apm-hooks.json").read_text(encoding="utf-8"))
        assert "portable-pkg" in json.dumps(sidecar)
        assert "$HOME/.claude/hooks/portable-pkg" in json.dumps(sidecar)

    def test_two_home_round_trip_produces_no_diff(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package = _package(tmp_path, _nested({"command": "sh ./run.sh"}))
        _install("claude", package, home)
        home_b = tmp_path / "home-b"
        shutil.copytree(home, home_b)
        _use_home(monkeypatch, home_b)

        _install("claude", package, home_b)

        for name in ("settings.json", "apm-hooks.json"):
            assert (home_b / ".claude" / name).read_bytes() == (
                home / ".claude" / name
            ).read_bytes()


def test_splice_references_rejects_overlapping_edits() -> None:
    assert splice_references("a b c", [(4, 5, "z"), (0, 1, "x")]) == "x b z"
    with pytest.raises(ValueError, match="overlapping"):
        splice_references("abcdef", [(0, 3, "x"), (2, 4, "y")])
