"""Bundle uninstall keeps a script that a user hook still runs, however it is spelled (#5106).

Before removing a local bundle's hook scripts, ``apm uninstall <bundle>`` refuses
when an unowned hook still runs one of them (#4679, narrowed to handler positions
by #5136). Each cell writes one user hook that RUNS the bundle's script through a
different spelling, then checks that uninstall refuses, keeps the script and its
lock claim, and changes nothing but the pending record (#5291 owns that write).
Required cells taken from QL's #5290 draft; its other cells stay there.
Owner: install/hook_script_refs.py through install/local_bundle_hook_trust.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import MarketplacePackWorld, output, tree_bytes, write
from tests.integration.test_local_bundle_hooks_lifecycle import (
    _allow,
    _claims_of,
    _copilot_bundle_consumer,
    _packed_bundle,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-local-bundle-hook-trust", "P-INV-I7"),
]


def _handler(bash: str) -> dict:
    return {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": bash}]}}


def _script_dir(script: str) -> str:
    return script.rsplit("/", 1)[0]


SPELLINGS = {
    "absolute": lambda s, root: _handler(f"{root}/{s}"),
    "backslashes": lambda s, root: _handler(s.replace("/", "\\")),
    "cd-prefix": lambda s, root: _handler(f"cd {_script_dir(s)} && ./x.sh"),
    "dot-segment": lambda s, root: _handler(s.replace("/scripts/", "/scripts/./")),
    "dot-slash": lambda s, root: _handler(f"./{s}"),
    "double-slash": lambda s, root: _handler(s.replace("/scripts/", "/scripts//")),
    "quoted": lambda s, root: _handler(f'"{s}" --flag'),
    "sh-invocation": lambda s, root: _handler(f"sh {s}"),
}
PENDING = "apm_modules/.local-bundle-pending.json"


def _assert_refused_unchanged(consumer: Path, before: dict, script: str) -> None:
    """Nothing changes but the pending record, whose write is #5291's."""
    after = tree_bytes(consumer)
    assert {k: v for k, v in after.items() if k != PENDING} == {
        k: v for k, v in before.items() if k != PENDING
    }
    entry = json.loads(after[PENDING])["bundles"]["hookpkg"]
    recorded = json.loads(before[PENDING])["bundles"]["hookpkg"]
    assert {k: v for k, v in entry.items() if k != "retiring"} == recorded
    assert (consumer / script).is_file()
    assert script in _claims_of(consumer)


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_uninstall_bundle_keeps_a_script_a_user_hook_runs(
    spelling: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, f"spelling-{spelling}")
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]
    document = SPELLINGS[spelling](script, consumer.as_posix())
    write(consumer / ".github" / "hooks" / "user.json", json.dumps(document))
    before = tree_bytes(consumer)

    result = world.apm(consumer, "uninstall", "hookpkg", expect=None)
    text = output(result)

    assert (consumer / script).is_file(), f"uninstall removed a script a user hook runs:\n{text}"
    assert result.returncode == 1, text
    assert ".github/hooks/user.json" in text, text
    assert "Remove or repair those files, then retry" in text, text
    _assert_refused_unchanged(consumer, before, script)


def test_uninstall_bundle_keeps_a_script_a_user_hook_in_another_target_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5106: a user Claude hook that runs the bundle's Copilot script keeps it."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    _producer, bundle, key = _packed_bundle(world, tmp_path)
    consumer = world.consumer("cross-target", targets=("claude", "copilot"))
    _allow(consumer, key)
    world.apm(consumer, "install", str(bundle))
    (script,) = [c for c in _claims_of(consumer) if c.startswith(".github/") and c.endswith("x.sh")]
    settings = consumer / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    user = {"matcher": "Write", "hooks": [{"type": "command", "command": f"./{script}"}]}
    data["hooks"]["PreToolUse"].append(user)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    before = tree_bytes(consumer)

    result = world.apm(consumer, "uninstall", "hookpkg", expect=None)
    text = output(result)

    assert (consumer / script).is_file(), f"uninstall removed a script a user hook runs:\n{text}"
    assert result.returncode == 1, text
    assert ".claude/settings.json" in text, text
    _assert_refused_unchanged(consumer, before, script)


def test_uninstall_bundle_keeps_a_script_a_user_hook_runs_through_its_cwd(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5281: `./x.sh` run with `cwd` at the script's directory runs the bundle script (fixed by #5166)."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "spelling-cwd-field")
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]
    handler = {"type": "command", "bash": "./x.sh", "cwd": _script_dir(script)}
    document = {"version": 1, "hooks": {"preToolUse": [handler]}}
    write(consumer / ".github" / "hooks" / "user.json", json.dumps(document))
    before = tree_bytes(consumer)

    result = world.apm(consumer, "uninstall", "hookpkg", expect=None)
    text = output(result)

    assert (consumer / script).is_file(), f"uninstall removed a script a user hook runs:\n{text}"
    assert result.returncode == 1, text
    assert ".github/hooks/user.json" in text, text
    _assert_refused_unchanged(consumer, before, script)


def test_uninstall_bundle_refuses_when_a_user_hook_file_carries_a_bom(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A hook file the reader cannot parse (UTF-8 BOM, CRLF lines) may still run the script."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "bom-consumer")
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]
    path = consumer / ".github" / "hooks" / "user.json"
    body = json.dumps(_handler(script), indent=2).replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert b"\r\n" in path.read_bytes()
    before = tree_bytes(consumer)

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".github/hooks/user.json" in text, text
    _assert_refused_unchanged(consumer, before, script)


def test_a_refused_bundle_uninstall_writes_nothing(tmp_path: Path, apm_binary_path: Path) -> None:
    """I7 (#5291): the in-use refusal leaves every file byte-identical, the pending record too."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    consumer, _hook = _copilot_bundle_consumer(world, tmp_path, "refused-write")
    (script,) = [c for c in _claims_of(consumer) if c.endswith("/x.sh")]
    write(consumer / ".github" / "hooks" / "user.json", json.dumps(_handler(f"./{script}")))
    before = tree_bytes(consumer)

    text = output(world.apm(consumer, "uninstall", "hookpkg", expect=1))

    assert ".github/hooks/user.json" in text, text
    assert (consumer / script).is_file()
    after = tree_bytes(consumer)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert changed == [], f"refused uninstall wrote {changed}: {text}"
