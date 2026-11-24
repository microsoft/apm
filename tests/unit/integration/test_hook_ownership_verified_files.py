"""unmanaged_files counts APM's merged hook files only while ownership verifies (#5277)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.hook_ownership import (
    dependency_hook_source_marker,
    local_bundle_hook_source_marker,
    verified_apm_hook_files,
)
from apm_cli.security.executables import save_user_executables

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3")]

_ENTRY = {"matcher": "*", "hooks": [{"type": "command", "command": "echo p-exec"}]}
_USER = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
_KEY = "p-exec#1.0.0@sha256:" + "0" * 64
_BUNDLE = local_bundle_hook_source_marker("p-exec", _KEY)
_PAIR = frozenset({".claude/settings.json", ".claude/apm-hooks.json"})


def _lock() -> LockFile:
    lock = LockFile()
    lock.add_dependency(LockedDependency.from_dict({"repo_url": "org/pkg"}))
    return lock


def _marker() -> str:
    (locked,) = _lock().get_all_dependencies()
    return dependency_hook_source_marker(locked.to_dependency_ref())


def _approve(project: Path, executables: object) -> None:
    (project / "apm.yml").write_text(
        json.dumps({"name": "consumer", "executables": executables}), encoding="utf-8"
    )


def _deploy(project: Path, sidecar: object, settings: object | None = None) -> Path:
    claude = project / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    _approve(project, {"allow": {_KEY: {"hooks": True}}})
    if settings is None:
        settings = {"model": "x", "hooks": {"SessionStart": [dict(_ENTRY)]}}
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    raw = sidecar if isinstance(sidecar, str) else json.dumps(sidecar)
    (claude / "apm-hooks.json").write_text(raw, encoding="utf-8")
    return claude


def _owned(marker: str, count: int = 1) -> dict:
    return {"SessionStart": [{**_ENTRY, "_apm_source": marker}] * count}


def test_an_approved_local_bundle_pair_verifies_beside_other_settings(tmp_path: Path) -> None:
    _deploy(tmp_path, _owned(_BUNDLE))
    assert verified_apm_hook_files(tmp_path, _lock()) == _PAIR


@pytest.mark.parametrize(
    "executables",
    [
        {},
        {"allow": {_KEY: {"mcp": True}}},
        {"allow": {_KEY: {"hooks": True}}, "deny": {_KEY: {"hooks": True}}},
    ],
    ids=["not-granted", "granted-another-type", "denied"],
)
def test_a_local_bundle_pair_verifies_only_for_a_hooks_grant_in_apm_yml(
    tmp_path: Path, executables: object
) -> None:
    _deploy(tmp_path, _owned(_BUNDLE))
    _approve(tmp_path, executables)
    assert verified_apm_hook_files(tmp_path, _lock()) == frozenset()


def test_a_key_granted_only_at_user_scope_attests_no_local_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / "home" / "config.json"))
    save_user_executables({_KEY: {"hooks": True}}, {})
    _deploy(tmp_path, _owned(_BUNDLE))
    _approve(tmp_path, {})
    assert (tmp_path / "home" / "config.json").is_file()
    assert verified_apm_hook_files(tmp_path, _lock()) == frozenset()


def test_an_unreadable_apm_yml_attests_no_local_bundle(tmp_path: Path) -> None:
    _deploy(tmp_path, _owned(_BUNDLE))
    (tmp_path / "apm.yml").write_text("executables: [\n", encoding="utf-8")
    assert verified_apm_hook_files(tmp_path, _lock()) == frozenset()


def test_a_dependency_pair_verifies_only_for_a_locked_dependency(tmp_path: Path) -> None:
    _deploy(tmp_path, _owned(_marker()))
    assert verified_apm_hook_files(tmp_path, _lock()) == _PAIR
    assert verified_apm_hook_files(tmp_path, LockFile()) == frozenset()


@pytest.mark.parametrize(
    ("sidecar", "settings"),
    [
        (_owned("other/pkg"), None),
        (_owned("local-bundle::"), None),
        (_owned("local-bundle::x::y"), None),
        (_owned("local-bundle::p-exec"), None),
        (_owned(_BUNDLE), {"hooks": {"SessionStart": [_USER, dict(_ENTRY)]}}),
        (_owned(_BUNDLE), {"hooks": {"SessionStart": [dict(_ENTRY)], "PreToolUse": [_USER]}}),
        (_owned(_BUNDLE), {"hooks": {"SessionStart": [dict(_ENTRY), dict(_ENTRY)]}}),
        ({"SessionStart": [{**_ENTRY, "_apm_source": 7}]}, None),
        ({"SessionStart": [dict(_ENTRY)]}, None),
        (_owned(_BUNDLE, count=2), None),
        ({"SessionStart": [{**_ENTRY, "command": "x", "_apm_source": _BUNDLE}]}, None),
        (_owned(_BUNDLE), {"hooks": {}}),
        (_owned(_BUNDLE), {"hooks": []}),
        (_owned(_BUNDLE), {"hooks": {"SessionStart": {"0": _ENTRY}}}),
        ({"SessionStart": {"0": _ENTRY}}, None),
        ({"SessionStart": []}, None),
        ({}, None),
        ([], None),
        ('{"SessionStart": [], "SessionStart": []}', None),
        ('{"SessionStart": NaN}', None),
        ('{"SessionStart": [', None),
    ],
    ids=[
        "unlocked-dependency",
        "empty-bundle-slug",
        "forged-bundle-key",
        "bundle-without-key",
        "user-hook-in-owned-event",
        "event-the-sidecar-lacks",
        "more-present-than-owned",
        "non-string-marker",
        "unmarked-entry",
        "more-owned-than-present",
        "entry-not-in-config",
        "config-hooks-removed",
        "config-container-not-object",
        "config-event-not-list",
        "sidecar-event-not-list",
        "sidecar-event-empty",
        "sidecar-empty",
        "sidecar-not-object",
        "sidecar-duplicate-key",
        "sidecar-non-json-constant",
        "sidecar-truncated",
    ],
)
def test_an_unverifiable_pair_stays_unmanaged(
    tmp_path: Path, sidecar: object, settings: object | None
) -> None:
    _deploy(tmp_path, sidecar, settings)
    assert verified_apm_hook_files(tmp_path, _lock()) == frozenset()


def _link_sidecar(claude: Path) -> None:
    real = claude.parent / "elsewhere.json"
    (claude / "apm-hooks.json").replace(real)
    (claude / "apm-hooks.json").symlink_to(real)


def _missing_config(claude: Path) -> None:
    (claude / "settings.json").unlink()


def _fifo(name: str) -> Callable[[Path], None]:
    def damage(claude: Path) -> None:
        (claude / name).unlink()
        os.mkfifo(claude / name)

    return damage


@pytest.mark.parametrize(
    "damage",
    [_link_sidecar, _missing_config, _fifo("apm-hooks.json"), _fifo("settings.json")],
    ids=["linked", "no-config", "fifo-sidecar", "fifo-config"],
)
def test_a_pair_that_is_not_two_regular_files_stays_unmanaged(
    tmp_path: Path, damage: Callable[[Path], None]
) -> None:
    claude = _deploy(tmp_path, _owned(_BUNDLE))
    try:
        damage(claude)
    except (AttributeError, OSError):
        pytest.skip("symlinks or named pipes are unavailable")
    assert verified_apm_hook_files(tmp_path, _lock()) == frozenset()


def test_each_target_pair_verifies_on_its_own(tmp_path: Path) -> None:
    _deploy(tmp_path, _owned(_BUNDLE))
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    (cursor / "hooks.json").write_text('{"version": 1, "hooks": {"stop": []}}', encoding="utf-8")
    (cursor / "apm-hooks.json").write_text(json.dumps(_owned(_BUNDLE)), encoding="utf-8")
    assert verified_apm_hook_files(tmp_path, _lock()) == _PAIR
