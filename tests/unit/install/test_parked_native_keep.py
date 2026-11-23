"""A parked native Claude plan keeps only inert loose files, and audit still binds them (#5036).

PROMISE: P-INV-I4 -- a parked native replacement never keeps an executable
loose deployment (hooks, merged settings, bin/, MCP or LSP entries) alive; it
keeps only non-executable primitive files, and drift replay exempts only kept
values whose bytes the lock binds by hash (source: issue #5036 review).
owner: install/deployment_plan.py PackageDeploymentPlan.kept_loose_values,
carry_kept_loose_into_replay
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePlugin
from apm_cli.install.deployment_plan import PackageDeploymentPlan, carry_kept_loose_into_replay
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I4")]

_PRIOR = (
    ".agents/skills/demo",
    ".agents/skills/demo/SKILL.md",
    ".claude/skills/demo",
    ".claude/skills/demo/SKILL.md",
    ".claude/rules/style.md",
    ".claude/commands/run.md",
    ".claude/agents/helper.md",
    ".claude/hooks/demo/guard.sh",
    ".claude/settings.json",
    ".claude/apm-hooks.json",
    ".claude/bin/tool",
    ".claude/skills/demo/bin/tool",
    ".claude/skills/demo/.claude-plugin/plugin.json",
)
_INERT = (
    ".claude/skills/demo",
    ".claude/skills/demo/SKILL.md",
    ".claude/rules/style.md",
    ".claude/commands/run.md",
    ".claude/agents/helper.md",
)


def _plan(
    *, parked: str | None, harnesses: frozenset[str] = frozenset({"claude"})
) -> PackageDeploymentPlan:
    native = ClaudeNativePlugin(
        "pkg", "demo", Path("/src"), "sha256:x", parked=parked, harnesses=harnesses
    )
    names = ("claude", "codex", *sorted(harnesses - {"claude"}))
    selection = SimpleNamespace(targets=[KNOWN_TARGETS[name] for name in names])
    return PackageDeploymentPlan(selection, claude_native=native)


@pytest.mark.parametrize("user_scope", [True, False], ids=["user", "project"])
def test_a_parked_plan_keeps_only_inert_loose_files(user_scope: bool) -> None:
    kept = _plan(parked="Parked native plugin pkg: denied").kept_loose_values(
        _PRIOR, user_scope=user_scope
    )

    assert sorted(kept) == sorted(_INERT)


def test_a_root_a_loose_target_still_serves_is_not_kept() -> None:
    # Copilot routed natively and Codex loose share .agents/skills; Codex
    # rewrites those files in this install, so they are not kept as they were.
    plan = _plan(
        parked="Parked native plugin pkg: denied", harnesses=frozenset({"claude", "copilot"})
    )

    kept = plan.kept_loose_values((*_PRIOR, ".github/prompts/p.prompt.md"), user_scope=False)

    assert sorted(kept) == sorted((*_INERT, ".github/prompts/p.prompt.md"))


def test_an_admitted_plan_keeps_nothing() -> None:
    assert _plan(parked=None).kept_loose_values(_PRIOR, user_scope=True) == ()


def test_replay_carries_only_kept_values_the_lock_binds_by_hash(tmp_path: Path) -> None:
    live, scratch = tmp_path / "live", tmp_path / "scratch"
    for value in (".claude/skills/demo/SKILL.md", ".claude/skills/demo/notes.md"):
        (live / value).parent.mkdir(parents=True, exist_ok=True)
        (live / value).write_text(value, encoding="ascii")
    lock_dep = SimpleNamespace(
        deployed_files=[
            ".claude/skills/demo",
            ".claude/skills/demo/SKILL.md",
            ".claude/skills/demo/notes.md",
        ],
        deployed_file_hashes={".claude/skills/demo/SKILL.md": "sha256:abc"},
    )
    result = {"deployment_plan": _plan(parked="Parked native plugin pkg: denied")}

    carry_kept_loose_into_replay(result, lock_dep, live, scratch, True)

    assert (scratch / ".claude/skills/demo/SKILL.md").is_file()
    # A kept value with no recorded hash stays a replay finding (orphaned).
    assert not (scratch / ".claude/skills/demo/notes.md").exists()


def test_replay_never_carries_a_kept_claim_outside_its_roots(tmp_path: Path) -> None:
    live, scratch = tmp_path / "home" / "live", tmp_path / "home" / "scratch"
    (live / ".claude/skills/demo").mkdir(parents=True)
    (live / ".claude/skills/demo/SKILL.md").write_text("ok", encoding="ascii")
    victim = tmp_path / "home" / "victim"
    victim.write_text("user bytes", encoding="ascii")
    escape = ".claude/skills/../../../victim"
    lock_dep = SimpleNamespace(
        deployed_files=[".claude/skills/demo/SKILL.md", escape],
        deployed_file_hashes={".claude/skills/demo/SKILL.md": "sha256:a", escape: "sha256:b"},
    )
    result = {"deployment_plan": _plan(parked="Parked native plugin pkg: denied")}

    carry_kept_loose_into_replay(result, lock_dep, live, scratch, True)

    assert victim.read_text(encoding="ascii") == "user bytes"
    assert sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*") if p.is_file()) == [
        ".claude/skills/demo/SKILL.md"
    ]


def test_replay_never_carries_a_kept_value_through_a_symlinked_parent(tmp_path: Path) -> None:
    live, scratch = tmp_path / "live", tmp_path / "scratch"
    (live / "elsewhere/skills/demo").mkdir(parents=True)
    (live / "elsewhere/skills/demo/SKILL.md").write_text("edited", encoding="ascii")
    (live / ".claude").symlink_to(live / "elsewhere", target_is_directory=True)
    lock_dep = SimpleNamespace(
        deployed_files=[".claude/skills/demo/SKILL.md"],
        deployed_file_hashes={".claude/skills/demo/SKILL.md": "sha256:a"},
    )
    result = {"deployment_plan": _plan(parked="Parked native plugin pkg: denied")}

    carry_kept_loose_into_replay(result, lock_dep, live, scratch, True)

    # Never stand in for the replay: drift then reports the linked copy.
    assert not (scratch / ".claude/skills/demo/SKILL.md").exists()
