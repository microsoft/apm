"""Merged hook config entries matching a withheld package's own hook are named, read-only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.withheld_hooks import HookPathsUnknown
from apm_cli.install.withheld_merged_hooks import withheld_merged_hook_entries
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-executable-item-admission")]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo h"}]}]}}
_CONFIG = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}
_CURSOR = ".cursor/hooks.json"


def _deployed(tmp_path: Path, names: tuple[str, ...]) -> tuple[SimpleNamespace, Path, list]:
    """Merge the package's hook with the real integrator, as an earlier install did."""
    package = tmp_path / "modules" / "h-hook"
    (package / "hooks").mkdir(parents=True)
    (package / "hooks" / "session.json").write_text(json.dumps(_HOOK), encoding="utf-8")
    info = SimpleNamespace(install_path=package, package=SimpleNamespace(name="h-hook"))
    project = tmp_path / "project"
    targets = [KNOWN_TARGETS[name] for name in names]
    for target in targets:
        (project / target.root_dir).mkdir(parents=True, exist_ok=True)
        HookIntegrator().integrate_hooks_for_target(target, info, project)
    return info, project, targets


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (("claude", "codex"), [f"{_CONFIG[n]}: SessionStart" for n in ("claude", "codex")]),
        (("cursor",), [f"{_CURSOR}: sessionStart"]),
    ],
    ids=["claude-codex", "cursor"],
)
@pytest.mark.parametrize("sidecar", [True, False], ids=["sidecar", "no-sidecar"])
def test_an_entry_matching_the_withheld_hook_is_named(
    tmp_path: Path, names: tuple[str, ...], expected: list[str], sidecar: bool
) -> None:
    info, project, targets = _deployed(tmp_path, names)
    if not sidecar:
        for path in project.rglob("apm-hooks.json"):
            path.unlink()
    before = {path: path.read_bytes() for path in project.rglob("*.json")}

    found = withheld_merged_hook_entries(info, project, targets)

    assert found == sorted(expected)
    assert {path: path.read_bytes() for path in project.rglob("*.json")} == before


def test_a_users_own_or_another_sources_entry_is_not_named(tmp_path: Path) -> None:
    info, project, targets = _deployed(tmp_path, ("claude",))
    settings = project / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    entry = data["hooks"]["SessionStart"][0]
    data["hooks"]["SessionStart"] = [
        {**entry, "hooks": [{"type": "command", "command": "echo mine"}]},
        {**entry, "_apm_source": "acme/other"},
    ]
    settings.write_text(json.dumps(data), encoding="utf-8")
    for path in project.rglob("apm-hooks.json"):
        path.unlink()

    assert withheld_merged_hook_entries(info, project, targets) == []
    assert json.loads(settings.read_text(encoding="utf-8")) == data


def test_an_unreadable_merged_config_fails_closed(tmp_path: Path) -> None:
    info, project, targets = _deployed(tmp_path, ("claude",))
    settings = project / ".claude" / "settings.json"
    settings.write_text("{not json", encoding="utf-8")

    with pytest.raises(HookPathsUnknown, match=r"\.claude/settings\.json"):
        withheld_merged_hook_entries(info, project, targets)

    assert settings.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("legacy_marker", [False, True], ids=["unattributed", "legacy-marker"])
def test_the_audit_scan_names_a_withheld_packages_merged_entry(
    tmp_path: Path, legacy_marker: bool
) -> None:
    from unittest.mock import patch

    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.drift import _build_package_info
    from apm_cli.install.withheld_hooks import locked_withheld_hook_files

    dep = LockedDependency(repo_url="acme/h-hook", resolved_commit="a" * 40, exec_status="denied")
    lock = LockFile()
    lock.add_dependency(dep)
    modules = tmp_path / "apm_modules"
    root = dep.to_dependency_ref().get_install_path(modules)
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "session.json").write_text(json.dumps(_HOOK), encoding="utf-8")
    (root / "apm.yml").write_text("name: h-hook\nversion: 1.0.0\n", encoding="utf-8")
    project = tmp_path / "project"
    claude = KNOWN_TARGETS["claude"]
    (project / claude.root_dir).mkdir(parents=True)
    lock.write(project / "apm.lock.yaml")
    HookIntegrator().integrate_hooks_for_target(claude, _build_package_info(dep, root), project)
    if legacy_marker:
        sidecar = project / ".claude" / "apm-hooks.json"
        owners = json.loads(sidecar.read_text(encoding="utf-8"))
        for entries in owners.values():
            for entry in entries:
                entry["_apm_source"] = "h-hook"
        sidecar.write_text(json.dumps(owners), encoding="utf-8")
    else:
        for path in project.rglob("apm-hooks.json"):
            path.unlink()

    with patch("apm_cli.install.exec_gate.current_exec_decision", return_value=("denied", ())):
        found = locked_withheld_hook_files(lock, modules, lambda: None, project, [claude])

    assert [(path, key) for path, key, _ in found] == [
        (".claude/settings.json: SessionStart", "acme/h-hook")
    ]
    assert "not attributed to another source" in found[0][2]
    assert (project / ".claude" / "settings.json").is_file()


def test_a_merged_config_reached_through_a_symlink_is_never_named_for_editing(
    tmp_path: Path,
) -> None:
    info, project, targets = _deployed(tmp_path, ("claude",))
    settings = project / ".claude" / "settings.json"
    external = tmp_path / "external-settings.json"
    external.write_bytes(settings.read_bytes())
    settings.unlink()
    settings.symlink_to(external)

    with pytest.raises(HookPathsUnknown, match="through a symlink"):
        withheld_merged_hook_entries(info, project, targets)

    assert settings.is_symlink()


def test_a_malformed_event_list_fails_closed(tmp_path: Path) -> None:
    info, project, targets = _deployed(tmp_path, ("claude",))
    settings = project / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["Stop"] = None
    settings.write_text(json.dumps(data), encoding="utf-8")
    (project / ".claude" / "apm-hooks.json").write_text(json.dumps({"Stop": []}), "utf-8")

    with pytest.raises(HookPathsUnknown, match=r"\.claude/settings\.json"):
        withheld_merged_hook_entries(info, project, targets)

    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"]["Stop"] is None
