"""Regression tests for #3249 -- untyped flat hook handlers in Codex hooks.

A flat hook entry may omit ``type`` (valid in Cursor, where it defaults to
``command``).  The Codex render wrapped such entries into Codex's
``{"matcher", "hooks": [...]}`` groups but left the handler without the
``type`` discriminator that Codex's ``HookHandlerConfig`` requires.

These tests assert the post-fix contract:
- Untyped flat command entries render as ``"type": "command"`` handlers in
  ``.codex/hooks.json`` and its ownership sidecar, both standalone and when
  merged next to a Codex-shaped hook file from the same package.
- Explicit handler types and untyped entries that are not command handlers
  are preserved unchanged.
- Untyped handlers already inside a nested Codex source group are unchanged.
- Reinstalling over APM-owned output from older installs -- the original flat
  shape and the nested-untyped shape -- leaves one typed group, including
  content-matched healing of stale root-package entries.
- Manual, unowned hooks are neither rewritten nor removed.
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

# Exact flat hook file from the #3249 report: no ``type`` on the handler.
_FLAT_UNTYPED_HOOK = {
    "version": 1,
    "hooks": {
        "PreToolUse": [
            {
                "command": 'node "${PLUGIN_ROOT}/.apm/hooks/scripts/gate.mjs"',
                "matcher": "Write",
                "timeout": 10,
            }
        ]
    },
}

_CODEX_SHAPED_HOOK = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo native", "timeout": 5}],
            }
        ]
    }
}

_DEPLOYED_COMMAND = 'node ".codex/hooks/demo-pkg/.apm/hooks/scripts/gate.mjs"'

_EXPECTED_FLAT_GROUP = {
    "matcher": "Write",
    "hooks": [{"type": "command", "command": _DEPLOYED_COMMAND, "timeout": 10}],
}

# Nested output written by installs before #3249 typed flat command entries.
_LEGACY_NESTED_GROUP = {
    "matcher": "Write",
    "hooks": [{"command": _DEPLOYED_COMMAND, "timeout": 10}],
}

# Flat output written by installs before Codex native nesting (#3060).
_LEGACY_FLAT_ENTRY = {"command": _DEPLOYED_COMMAND, "matcher": "Write", "timeout": 10}


def _write_package(project_root: Path, hook_files: dict[str, dict]) -> PackageInfo:
    """Materialise the #3249 package layout with the given hook files."""
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


def _integrate(project_root: Path, package_info: PackageInfo) -> None:
    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["codex"],
        package_info,
        project_root,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _without_ownership(entries: list) -> list:
    """Drop APM ownership markers so sidecar entries compare to hooks.json."""
    return [{k: v for k, v in entry.items() if k != "_apm_source"} for entry in entries]


def _write_existing(codex_dir: Path, hooks: list, owned: list) -> None:
    """Seed hooks.json and the ownership sidecar with prior install output."""
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": hooks}}), encoding="utf-8"
    )
    (codex_dir / "apm-hooks.json").write_text(json.dumps({"PreToolUse": owned}), encoding="utf-8")


def test_standalone_flat_untyped_entry_gets_command_type(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})

    _integrate(tmp_path, package_info)

    config = _read_json(tmp_path / ".codex" / "hooks.json")
    assert config["hooks"] == {"PreToolUse": [_EXPECTED_FLAT_GROUP]}
    sidecar = _read_json(tmp_path / ".codex" / "apm-hooks.json")
    assert _without_ownership(sidecar["PreToolUse"]) == [_EXPECTED_FLAT_GROUP]


def test_flat_untyped_entry_merged_next_to_codex_shaped_file(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    package_info = _write_package(
        tmp_path,
        {"codex.json": _CODEX_SHAPED_HOOK, "flat.json": _FLAT_UNTYPED_HOOK},
    )

    _integrate(tmp_path, package_info)

    config = _read_json(tmp_path / ".codex" / "hooks.json")
    expected = [_CODEX_SHAPED_HOOK["hooks"]["PreToolUse"][0], _EXPECTED_FLAT_GROUP]
    assert config["hooks"] == {"PreToolUse": expected}
    sidecar = _read_json(tmp_path / ".codex" / "apm-hooks.json")
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
            {"powershell": "Write-Output windows"},
            {"type": "command", "command": "Write-Output windows"},
            id="untyped-powershell-command",
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
            {"type": "mcp_tool", "server": "s", "tool": "t"},
            {"type": "mcp_tool", "server": "s", "tool": "t"},
            id="explicit-mcp-tool",
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
    (tmp_path / ".codex").mkdir()
    package_info = _write_package(
        tmp_path, {"edge.json": {"hooks": {"PreToolUse": [source_entry]}}}
    )

    _integrate(tmp_path, package_info)

    config = _read_json(tmp_path / ".codex" / "hooks.json")
    assert config["hooks"]["PreToolUse"] == [{"hooks": [expected_handler]}]


def test_untyped_handler_inside_nested_codex_group_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    nested = {"matcher": "Edit", "hooks": [{"command": "echo nested"}]}
    package_info = _write_package(tmp_path, {"nested.json": {"hooks": {"PostToolUse": [nested]}}})

    _integrate(tmp_path, package_info)

    config = _read_json(tmp_path / ".codex" / "hooks.json")
    assert config["hooks"] == {"PostToolUse": [nested]}


@pytest.mark.parametrize(
    "legacy_entry",
    [
        pytest.param(_LEGACY_NESTED_GROUP, id="nested-untyped"),
        pytest.param(_LEGACY_FLAT_ENTRY, id="flat"),
    ],
)
def test_reinstall_replaces_owned_output_written_before_fix(
    tmp_path: Path, legacy_entry: dict
) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})
    _write_existing(codex_dir, [legacy_entry], [{**legacy_entry, "_apm_source": _PACKAGE_NAME}])

    _integrate(tmp_path, package_info)
    _integrate(tmp_path, package_info)

    config = _read_json(codex_dir / "hooks.json")
    assert config["hooks"] == {"PreToolUse": [_EXPECTED_FLAT_GROUP]}
    sidecar = _read_json(codex_dir / "apm-hooks.json")
    assert sidecar == {"PreToolUse": [{**_EXPECTED_FLAT_GROUP, "_apm_source": _PACKAGE_NAME}]}


def test_unowned_untyped_hook_is_not_rewritten_or_removed(tmp_path: Path) -> None:
    """A manual hook identical to old APM output stays when no ownership proves it."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    package_info = _write_package(tmp_path, {"flat.json": _FLAT_UNTYPED_HOOK})
    manual = {"matcher": "Read", "hooks": [{"command": "echo manual"}]}
    _write_existing(codex_dir, [manual, _LEGACY_NESTED_GROUP], [])

    _integrate(tmp_path, package_info)
    _integrate(tmp_path, package_info)

    config = _read_json(codex_dir / "hooks.json")
    assert config["hooks"] == {"PreToolUse": [manual, _LEGACY_NESTED_GROUP, _EXPECTED_FLAT_GROUP]}
    sidecar = _read_json(codex_dir / "apm-hooks.json")
    assert sidecar == {"PreToolUse": [{**_EXPECTED_FLAT_GROUP, "_apm_source": _PACKAGE_NAME}]}


@pytest.mark.parametrize(
    "legacy_entry",
    [
        pytest.param(
            {"matcher": "Write", "hooks": [{"command": "echo root"}]}, id="nested-untyped"
        ),
        pytest.param({"command": "echo root", "matcher": "Write"}, id="flat"),
    ],
)
def test_stale_root_source_output_is_still_healed(tmp_path: Path, legacy_entry: dict) -> None:
    """Content-matched healing must still recognise output written before the fix."""
    (tmp_path / "apm.yml").write_text("name: consumer\nversion: 0.0.1\n", encoding="utf-8")
    hooks_dir = tmp_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "flat.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "echo root", "matcher": "Write"}]}}),
        encoding="utf-8",
    )
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    _write_existing(codex_dir, [legacy_entry], [{**legacy_entry, "_apm_source": "old-root-name"}])
    root_package = PackageInfo(
        package=APMPackage(name="consumer", version="0.0.1"),
        install_path=tmp_path,
    )

    _integrate(tmp_path, root_package)

    config = _read_json(codex_dir / "hooks.json")
    assert config["hooks"] == {
        "PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "echo root"}]}]
    }
    sidecar = _read_json(codex_dir / "apm-hooks.json")
    assert sidecar == {
        "PreToolUse": [{**config["hooks"]["PreToolUse"][0], "_apm_source": "_local/consumer"}]
    }
