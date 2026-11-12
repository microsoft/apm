"""D1: a gated push/PR may only follow read-only segments in one tool call."""

from __future__ import annotations

import json

import gate
import preflight
import pytest
from conftest import Sandbox, lib

CWD = "/work/repo"


def _actions(command: str) -> list[gate.Action]:
    return gate.classify("bash", {"command": command}, CWD)


@pytest.mark.parametrize(
    ("command", "segment"),
    [
        (
            "git commit --allow-empty -m x && git push origin HEAD:refs/heads/e2e-chain",
            "git commit",
        ),
        ("git checkout other && git push", "git checkout other"),
        ("git reset --hard HEAD~1 && git push -f", "git reset --hard HEAD~1"),
        ("git add -A; git commit -m x; git push", "git add -A"),
        ("make build && gh pr create --fill", "make build"),
        ("git remote add x url && git push x HEAD", "git remote add x url"),
        ("git branch -D old && git push", "git branch -D old"),
        ("git config user.name x && git push", "git config user.name x"),
        ("ls | git push", "ls"),
        ("FOO=1 git commit -m x && git push", "FOO=1 git commit -m x"),
        ("git commit -m x && bash -c 'git push'", "git commit -m x"),
        ("bash -c 'git commit -m x && git push'", "git commit -m x"),
        ("git commit -m x; echo `git push`", "git commit -m x"),
        ("git stash && gh pr ready 3", "git stash"),
    ],
)
def test_mutating_segment_before_publish_is_denied(command: str, segment: str) -> None:
    actions = _actions(command)
    assert actions
    for action in actions:
        assert action.deny is not None
        assert "as its own tool call after preflight" in action.deny
        assert f"this call runs '{segment}" in action.deny
        assert action.fix and "commit in its own tool call, run `<RUNNER>`, then run" in action.fix


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD",
        "cd sub && git push",
        "pushd sub && git push && popd",
        "true && git push",
        "echo pushing; printf 'x' && git push",
        "git status && git push",
        "git fetch origin && git log -1 && git diff --stat && git push origin HEAD",
        "git show HEAD && git rev-parse HEAD && gh pr create --fill",
        "git remote -v && git push",
        "git branch --show-current && git push",
        "git config --get user.name && git push",
        "git push --dry-run && git push",
        "git -C . --no-pager status && git push",
        "git push && git commit -m after",
    ],
)
def test_read_only_prefixes_keep_publish_bindable(command: str) -> None:
    actions = _actions(command)
    assert actions
    assert all(a.deny is None for a in actions), [a.deny for a in actions]


def _payload(box: Sandbox, command: str) -> str:
    return json.dumps({"cwd": str(box.root), "toolName": "bash", "toolArgs": {"command": command}})


def test_gate_denies_chained_push_even_with_fresh_receipt(sandbox: Sandbox, fake_policy) -> None:
    assert preflight.run([], policy=fake_policy, cwd=sandbox.root) == 0
    cfg = gate.GateConfig(scripts_dir=sandbox.root / lib.DEPLOYED_SCRIPTS_REL)
    assert gate.evaluate(_payload(sandbox, "git status && git push origin HEAD"), {}, cfg) is None
    for command in (
        "git commit --allow-empty -m x && git push origin HEAD:refs/heads/e2e-chain",
        "git checkout -q main && git push",
        "git reset --hard HEAD~1 && git push -f",
    ):
        out = gate.evaluate(_payload(sandbox, command), {}, cfg)
        assert out is not None, command
        reason = json.loads(out)["permissionDecisionReason"]
        assert "as its own tool call after preflight" in reason
