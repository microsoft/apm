"""Regression tests for #3130 -- untyped flat hook handlers in Claude settings.

A flat hook entry may omit ``type`` (valid in Cursor, where it defaults to
``command``).  The Claude render wrapped such entries into Claude's
``{"matcher", "hooks": [...]}`` groups but left the handler without the
``type`` field that Claude's hook schema requires.

These tests assert the post-fix contract:
- Untyped flat command entries render as ``"type": "command"`` handlers in
  ``.claude/settings.json`` and its ownership sidecar, both standalone and
  when merged next to a Claude-shaped hook file from the same package.
- Explicit handler types and untyped entries that are not command handlers
  are preserved unchanged.
- Untyped handlers already inside a nested Claude group are left unchanged.
- Other targets do not receive the Claude-only default.
- Reinstalling over settings written before the fix leaves one typed group,
  including content-matched healing of stale root-package entries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = pytest.mark.component

_PACKAGE_NAME = "demo-pkg"

# Exact flat hook file from the #3130 report: no ``type`` on the handler.
_FLAT_UNTYPED_HOOK = {
    "version": 1,
    "hooks": {
        "preToolUse": [
            {
                "command": 'node "${PLUGIN_ROOT}/.apm/hooks/scripts/gate.mjs"',
                "matcher": "Write",
                "timeout": 10,
            }
        ]
    },
}

_CLAUDE_SHAPED_HOOK = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo native", "timeout": 5}],
            }
        ]
    }
}

_EXPECTED_FLAT_GROUP = {
    "matcher": "Write",
    "hooks": [
        {
            "type": "command",
            "command": (
                'node "${CLAUDE_PROJECT_DIR}/.claude/hooks/demo-pkg/.apm/hooks/scripts/gate.mjs"'
            ),
            "timeout": 10,
        }
    ],
}


def _write_package(project_root: Path, hook_files: dict[str, dict]) -> PackageInfo:
    """Materialise the #3130 package layout with the given hook files."""
    package_root = project_root / "apm_modules" / "owner" / _PACKAGE_NAME
    hooks_dir = package_root / ".apm" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True)
    (hooks_dir / "scripts" / "gate.mjs").write_text("process.exit(0);\n", encoding="utf-8")
    for name, document in hook_files.items():
        (hooks_dir / name).write_text(json.dumps(document), encoding="utf-8")
    return PackageInfo(
        package=APMPackage(name=_PACKAGE_NAME, version="0.0.1"),
        install_path=package_root,
    )


def _integrate(project_root: Path, package_info: PackageInfo, target: str) -> None:
    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS[target],
        package_info,
        project_root,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _without_ownership(entries: list) -> list:
    """Drop APM ownership markers so sidecar entries compare to settings."""
    return [{k: v for k, v in entry.items() if k != "_apm_source"} for entry in entries]


def test_standalone_flat_untyped_entry_gets_command_type(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})

    _integrate(tmp_path, package_info, "claude")

    settings = _read_json(tmp_path / ".claude" / "settings.json")
    assert settings["hooks"] == {"PreToolUse": [_EXPECTED_FLAT_GROUP]}
    sidecar = _read_json(tmp_path / ".claude" / "apm-hooks.json")
    assert _without_ownership(sidecar["PreToolUse"]) == [_EXPECTED_FLAT_GROUP]


def test_flat_untyped_entry_merged_next_to_claude_shaped_file(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    package_info = _write_package(
        tmp_path,
        {"claude.json": _CLAUDE_SHAPED_HOOK, "flat.json": _FLAT_UNTYPED_HOOK},
    )

    _integrate(tmp_path, package_info, "claude")

    settings = _read_json(tmp_path / ".claude" / "settings.json")
    expected = [_CLAUDE_SHAPED_HOOK["hooks"]["PreToolUse"][0], _EXPECTED_FLAT_GROUP]
    assert settings["hooks"] == {"PreToolUse": expected}
    sidecar = _read_json(tmp_path / ".claude" / "apm-hooks.json")
    assert _without_ownership(sidecar["PreToolUse"]) == expected


@pytest.mark.parametrize(
    ("source_entry", "expected_handler"),
    [
        pytest.param(
            {"bash": "echo posix"},
            {"type": "command", "command": "echo posix"},
            id="untyped-bash-command",
        ),
        pytest.param(
            {"type": "command", "command": "echo typed"},
            {"type": "command", "command": "echo typed"},
            id="explicit-command",
        ),
        pytest.param(
            {"type": "prompt", "prompt": "Review the change"},
            {"type": "prompt", "prompt": "Review the change"},
            id="explicit-prompt",
        ),
        pytest.param(
            {"foo": 1},
            {"foo": 1},
            id="untyped-without-command",
        ),
    ],
)
def test_flat_handler_types_are_defaulted_only_for_commands(
    tmp_path: Path, source_entry: dict, expected_handler: dict
) -> None:
    (tmp_path / ".claude").mkdir()
    package_info = _write_package(
        tmp_path, {"edge.json": {"hooks": {"PreToolUse": [source_entry]}}}
    )

    _integrate(tmp_path, package_info, "claude")

    settings = _read_json(tmp_path / ".claude" / "settings.json")
    assert settings["hooks"]["PreToolUse"] == [{"matcher": "*", "hooks": [expected_handler]}]


def test_untyped_handler_inside_nested_claude_group_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    nested = {"matcher": "Edit", "hooks": [{"command": "echo nested"}]}
    package_info = _write_package(tmp_path, {"nested.json": {"hooks": {"PostToolUse": [nested]}}})

    _integrate(tmp_path, package_info, "claude")

    settings = _read_json(tmp_path / ".claude" / "settings.json")
    assert settings["hooks"] == {"PostToolUse": [nested]}


@pytest.mark.parametrize(
    ("target", "config_dir", "config_file"),
    [
        pytest.param("codex", ".codex", "hooks.json", id="codex"),
        pytest.param("cursor", ".cursor", "hooks.json", id="cursor"),
    ],
)
def test_claude_handler_default_does_not_reach_other_targets(
    tmp_path: Path, target: str, config_dir: str, config_file: str
) -> None:
    (tmp_path / config_dir).mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})

    _integrate(tmp_path, package_info, target)

    config = _read_json(tmp_path / config_dir / config_file)
    rendered = json.dumps(config["hooks"])
    assert '"type"' not in rendered
    assert "gate.mjs" in rendered


def test_reinstall_replaces_untyped_group_written_before_fix(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})
    legacy_group = {
        "matcher": "Write",
        "hooks": [
            {
                "command": _EXPECTED_FLAT_GROUP["hooks"][0]["command"],
                "timeout": 10,
            }
        ],
    }
    (claude_dir / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [legacy_group]}}), encoding="utf-8"
    )
    (claude_dir / "apm-hooks.json").write_text(
        json.dumps({"PreToolUse": [{**legacy_group, "_apm_source": _PACKAGE_NAME}]}),
        encoding="utf-8",
    )

    _integrate(tmp_path, package_info, "claude")
    _integrate(tmp_path, package_info, "claude")

    settings = _read_json(claude_dir / "settings.json")
    assert settings["hooks"] == {"PreToolUse": [_EXPECTED_FLAT_GROUP]}


def test_stale_root_source_untyped_group_is_still_healed(tmp_path: Path) -> None:
    """Content-matched healing must still recognise groups written before the fix."""
    (tmp_path / "apm.yml").write_text("name: consumer\nversion: 0.0.1\n", encoding="utf-8")
    hooks_dir = tmp_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "flat.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "echo root", "matcher": "Write"}]}}),
        encoding="utf-8",
    )
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    legacy_group = {"matcher": "Write", "hooks": [{"command": "echo root"}]}
    (claude_dir / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [legacy_group]}}), encoding="utf-8"
    )
    (claude_dir / "apm-hooks.json").write_text(
        json.dumps({"PreToolUse": [{**legacy_group, "_apm_source": "old-root-name"}]}),
        encoding="utf-8",
    )
    root_package = PackageInfo(
        package=APMPackage(name="consumer", version="0.0.1"),
        install_path=tmp_path,
    )

    _integrate(tmp_path, root_package, "claude")

    settings = _read_json(claude_dir / "settings.json")
    assert settings["hooks"] == {
        "PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "echo root"}]}]
    }
