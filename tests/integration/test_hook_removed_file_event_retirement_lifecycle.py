"""A hook file removed from its source retires its merged event entries (#3665).

Root ``.apm/hooks`` and a local hook package share Claude's merged
``settings.json`` with user-authored hooks. Deleting one hook file and
reinstalling must remove that file's event entries from ``settings.json`` and
the ownership sidecar, keep the user's hooks and the source's other events,
and leave ``apm audit --ci``, ``install --frozen`` and reinstall clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_GUARD = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "sh ./scripts/guard.sh"}]}
        ]
    }
}
_STOP = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo apm-stop"}]}]}}
_WELCOME = {
    "version": 1,
    "hooks": {"sessionStart": [{"type": "prompt", "prompt": "Read CONTRIBUTING.md first."}]},
}
_USER_HOOK = {"hooks": [{"type": "command", "command": "echo user-owned"}]}


def _write_hook(base: Path, name: str, document: dict) -> None:
    hooks = base / ".apm" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")


def _settings(chain: Chain) -> dict:
    return json.loads((chain.project / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _commands(chain: Chain, event: str) -> list[str]:
    return [
        handler.get("command") or handler.get("prompt")
        for group in _settings(chain).get("hooks", {}).get(event, [])
        for handler in group.get("hooks", [])
    ]


@pytest.mark.parametrize("source", ["root", "package"])
def test_removed_hook_file_drops_its_merged_event(
    tmp_path: Path, apm_binary_path: Path, source: str
) -> None:
    chain = Chain(tmp_path / source, apm_binary_path, dirs=(".claude", ".github"))
    manifest: dict = {"name": "hooks-chain", "version": "1.0.0", "targets": ["claude", "copilot"]}
    base = chain.project
    manifest["dependencies"] = {"apm": []}
    if source == "package":
        base = chain.project / "pkgs" / "hp"
        base.mkdir(parents=True)
        (base / "apm.yml").write_text("name: hp\nversion: 1.0.0\n", encoding="utf-8")
        manifest["dependencies"] = {"apm": ["./pkgs/hp"]}
    (chain.project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    _write_hook(base, "guard", _GUARD)
    (base / ".apm" / "hooks" / "scripts").mkdir()
    (base / ".apm" / "hooks" / "scripts" / "guard.sh").write_text("#!/bin/sh\necho g\n", "utf-8")
    _write_hook(base, "stop", _STOP)
    _write_hook(base, "welcome", _WELCOME)

    chain.ok("install", "--no-policy")
    assert len(_commands(chain, "PreToolUse")) == 1
    assert _commands(chain, "Stop") == ["echo apm-stop"]
    chain.ok("audit", "--ci", "--no-policy")

    settings = _settings(chain)
    settings["hooks"]["Stop"].append(_USER_HOOK)
    settings["model"] = "opus"
    (chain.project / ".claude" / "settings.json").write_text(json.dumps(settings), "utf-8")
    chain.ok("install", "--no-policy")
    assert sorted(_commands(chain, "Stop")) == ["echo apm-stop", "echo user-owned"]

    (base / ".apm" / "hooks" / "guard.json").unlink()
    (base / ".apm" / "hooks" / "stop.json").unlink()
    chain.ok("install", "--no-policy")
    assert "PreToolUse" not in _settings(chain).get("hooks", {})
    assert _commands(chain, "Stop") == ["echo user-owned"]
    assert _commands(chain, "SessionStart") == ["Read CONTRIBUTING.md first."]
    assert _settings(chain)["model"] == "opus"
    sidecar = json.loads((chain.project / ".claude" / "apm-hooks.json").read_text("utf-8"))
    assert set(sidecar) == {"SessionStart"}
    chain.ok("audit", "--ci", "--no-policy")

    settings_bytes = (chain.project / ".claude" / "settings.json").read_bytes()
    lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()
    chain.ok("install", "--frozen", "--no-policy")
    chain.ok("install", "--no-policy")
    assert (chain.project / ".claude" / "settings.json").read_bytes() == settings_bytes
    assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes
    chain.ok("audit", "--ci", "--no-policy")
