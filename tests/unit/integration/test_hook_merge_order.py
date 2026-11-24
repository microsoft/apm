"""A merged hook config has one canonical order, whatever order packages are added (#5285).

Installing hook packages one at a time appended each package's entries to the
shared config, so ``apm audit --ci`` saw drift against a full install that
integrated them in another order. The writer now sorts APM's entries inside
the slots they hold; user entries and events keep their places.
"""

from __future__ import annotations

import json
from itertools import permutations
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import _HOOK_EVENT_MAP, _MERGE_HOOK_TARGETS, HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-neutral-hook-contract", "P-LAW-idempotency.byte_stable"),
]

_TARGETS = ("claude", "cursor", "codex", "gemini")
_USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
# Cursor's strict schema: flat handlers and no foreign top-level keys.
_CURSOR_USER_HOOK = {"command": "echo mine"}


def _user_config(target: str, container: str) -> dict:
    event = _HOOK_EVENT_MAP.get(target, {}).get("PreToolUse", "PreToolUse")
    if target == "cursor":
        return {"version": 1, container: {event: [_CURSOR_USER_HOOK]}}
    return {"permissions": {"allow": ["Bash(ls)"]}, container: {event: [_USER_HOOK]}}


def _package(project: Path, name: str, extra_event: str) -> PackageInfo:
    root = project / "apm_modules" / "owner" / name
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    handler = {"type": "command", "command": f"echo {name}"}
    group = {"matcher": "*", "hooks": [handler]}
    document = {"hooks": {"PreToolUse": [group], extra_event: [group]}}
    (hooks / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
    return PackageInfo(package=APMPackage(name=name, version="1.0.0"), install_path=root)


def _install_in_order(project: Path, target: str, order: tuple[str, ...]) -> Path:
    config = _MERGE_HOOK_TARGETS[target]
    root = project / KNOWN_TARGETS[target].root_dir
    root.mkdir(parents=True)
    user = _user_config(target, config.event_container_key)
    (root / config.config_filename).write_text(json.dumps(user), encoding="utf-8")
    events = {"alpha": "PostToolUse", "beta": "Stop", "gamma": "SessionStart"}
    packages = {name: _package(project, name, events[name]) for name in events}
    for name in order:
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS[target], packages[name], project)
    return root


@pytest.mark.parametrize("target", _TARGETS)
def test_merged_hook_config_bytes_do_not_depend_on_addition_order(
    tmp_path: Path, target: str
) -> None:
    """#5285: every addition order writes the same config and sidecar bytes."""
    roots = {
        order: _install_in_order(tmp_path / "-".join(order), target, order)
        for order in permutations(("alpha", "beta", "gamma"))
    }

    canonical = roots[("alpha", "beta", "gamma")]
    names = sorted(path.name for path in canonical.glob("*.json"))
    assert names
    for order, root in roots.items():
        for name in names:
            assert (root / name).read_bytes() == (canonical / name).read_bytes(), (order, name)


@pytest.mark.parametrize("target", _TARGETS)
def test_canonical_order_keeps_user_hooks_in_place(tmp_path: Path, target: str) -> None:
    """#5285: APM's entries are ordered among themselves; the user's entry stays first."""
    config = _MERGE_HOOK_TARGETS[target]
    root = _install_in_order(tmp_path, target, ("gamma", "alpha", "beta"))
    document = json.loads((root / config.config_filename).read_text(encoding="utf-8"))

    container = config.event_container_key
    assert {k: v for k, v in document.items() if k != container} == {
        k: v for k, v in _user_config(target, container).items() if k != container
    }
    first = next(iter(document[container].values()))
    assert "echo mine" in json.dumps(first[0])
    commands = [json.dumps(entry) for entry in first[1:]]
    assert [name for name in ("alpha", "beta", "gamma") for c in commands if name in c] == [
        "alpha",
        "beta",
        "gamma",
    ]


@pytest.mark.parametrize("target", _TARGETS)
def test_reinstall_keeps_a_user_hook_between_owned_entries(tmp_path: Path, target: str) -> None:
    """#5285: reinstalling a package refills its own slots, so a user hook between
    two packages' entries keeps its place and the config bytes do not change."""
    config = _MERGE_HOOK_TARGETS[target]
    root = tmp_path / KNOWN_TARGETS[target].root_dir
    root.mkdir(parents=True)
    path = root / config.config_filename
    container = config.event_container_key
    event = _HOOK_EVENT_MAP.get(target, {}).get("PreToolUse", "PreToolUse")
    packages = {name: _package(tmp_path, name, "Stop") for name in ("alpha", "beta")}
    for name in ("alpha", "beta"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS[target], packages[name], tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    user = _CURSOR_USER_HOOK if target == "cursor" else _USER_HOOK
    document[container][event].insert(1, user)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in sorted(root.glob("*.json"))}

    HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS[target], packages["alpha"], tmp_path)

    assert {p.name: p.read_bytes() for p in sorted(root.glob("*.json"))} == before
    entries = json.loads(path.read_text(encoding="utf-8"))[container][event]
    assert "echo mine" in json.dumps(entries[1]), entries
