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


def _install_in_order(project: Path, target: str, order: tuple[str, ...]) -> dict[str, bytes]:
    config = _MERGE_HOOK_TARGETS[target]
    root = project / KNOWN_TARGETS[target].root_dir
    root.mkdir(parents=True)
    user = _user_config(target, config.event_container_key)
    (root / config.config_filename).write_text(json.dumps(user), encoding="utf-8")
    events = {"alpha": "PostToolUse", "beta": "Stop", "gamma": "SessionStart"}
    packages = {name: _package(project, name, events[name]) for name in events}
    for name in order:
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS[target], packages[name], project)
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(root.glob("*.json"))
    }


@pytest.mark.parametrize("target", _TARGETS)
def test_merged_hook_config_bytes_do_not_depend_on_addition_order(
    tmp_path: Path, target: str
) -> None:
    """#5285: every addition order writes the same config and sidecar bytes."""
    written = {
        order: _install_in_order(tmp_path / "-".join(order), target, order)
        for order in permutations(("alpha", "beta", "gamma"))
    }

    canonical = written[("alpha", "beta", "gamma")]
    assert len(canonical) >= 1
    for order, files in written.items():
        assert files == canonical, order
    first_rel, first_bytes = next(iter(canonical.items()))
    assert (tmp_path / "gamma-beta-alpha" / first_rel).read_bytes() == first_bytes


@pytest.mark.parametrize("target", _TARGETS)
def test_canonical_order_keeps_user_hooks_in_place(tmp_path: Path, target: str) -> None:
    """#5285: APM's entries are ordered among themselves; the user's entry stays first."""
    config = _MERGE_HOOK_TARGETS[target]
    _install_in_order(tmp_path, target, ("gamma", "alpha", "beta"))
    written = tmp_path / KNOWN_TARGETS[target].root_dir / config.config_filename
    document = json.loads(written.read_text(encoding="utf-8"))

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
