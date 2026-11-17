"""Removal reconciliation keeps executables-gate decisions (#3649).

``apm uninstall`` and ``apm prune`` rebuild hooks for surviving packages after
removing another one. The rebuild resolves trust through the same executables
ladder as ``apm install``: a survivor whose hooks are gated or denied stays
without hooks, while an allowed survivor gets its hooks back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._mcp_chain_kit import Chain

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_HOOK = {
    "hooks": {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo guard"}]}]
    }
}
_DENY_ALL_POLICY = "name: p\nversion: 1.0.0\nenforcement: block\nexecutables:\n  deny_all: true\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hook_commands(chain: Chain) -> list[str]:
    settings = chain.project / ".claude" / "settings.json"
    if not settings.exists():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command")
        for groups in data.get("hooks", {}).values()
        for group in groups
        for handler in group.get("hooks", [])
    ]


def _world(tmp_path: Path, binary: Path) -> Chain:
    chain = Chain(tmp_path / "world", binary, dirs=(".claude",))
    hooks_pkg = chain.project / "pkgs" / "guard"
    _write(hooks_pkg / "apm.yml", "name: guard\nversion: 1.0.0\n")
    _write(hooks_pkg / ".apm" / "hooks" / "guard.json", json.dumps(_HOOK))
    other = chain.project / "pkgs" / "notes"
    _write(other / "apm.yml", "name: notes\nversion: 1.0.0\n")
    _write(
        other / ".apm" / "skills" / "notes" / "SKILL.md",
        "---\nname: notes\ndescription: d\n---\nx\n",
    )
    return chain


def _remove_notes(chain: Chain, operation: str, extra: dict) -> None:
    if operation == "uninstall":
        chain.ok("uninstall", "./pkgs/notes")
    else:
        chain.write_manifest([], apm=["./pkgs/guard"], targets=["claude"], **extra)
        chain.ok("prune")


@pytest.mark.parametrize("operation", ["uninstall", "prune"])
@pytest.mark.parametrize(
    ("trust", "expected"),
    [("gated", []), ("allowed", ["echo guard"]), ("org-deny-all", [])],
)
def test_removal_rebuild_follows_executables_gate(
    tmp_path: Path, apm_binary_path: Path, operation: str, trust: str, expected: list[str]
) -> None:
    chain = _world(tmp_path, apm_binary_path)
    extra: dict = {"executables": {}}
    if trust == "allowed":
        extra = {"executables": {"allow": {"./pkgs/guard": {"hooks": True}}}}
    install_args: tuple[str, ...] = ("install", "--no-policy")
    if trust == "org-deny-all":
        chain.seed_org_policy(_DENY_ALL_POLICY)
        extra = {"executables": {"allow": {"./pkgs/guard": {"hooks": True}}}}
        install_args = ("install",)
    chain.write_manifest([], apm=["./pkgs/guard", "./pkgs/notes"], targets=["claude"], **extra)

    chain.ok(*install_args)
    assert _hook_commands(chain) == expected

    _remove_notes(chain, operation, extra)

    assert _hook_commands(chain) == expected
