"""Deny reasons are self-sufficient for an agent that never heard of preflight.

Every deny must carry: (a) a one-line why with HEAD sha7, (b) the exact
runner command at the deployed path, (c) failed check ids + log paths when
the receipt failed, (d) the human-only bypass. No draft/wait-for-CI advice:
agents open PRs ready for review and CI-only lanes simply run in CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gate
import preflight
import preflight_lib as lib
import pytest
from conftest import Sandbox, run_git

RUNNER_CMD = f"python3 {lib.DEPLOYED_SCRIPTS_REL}/preflight.py"


def _cfg(box: Sandbox, **kwargs) -> gate.GateConfig:
    return gate.GateConfig(scripts_dir=box.root / lib.DEPLOYED_SCRIPTS_REL, **kwargs)


def _deny(box: Sandbox, command: str, **kwargs) -> str:
    raw = json.dumps(
        {
            "sessionId": "s",
            "timestamp": 1,
            "cwd": str(box.root),
            "toolName": "bash",
            "toolArgs": {"command": command, "description": "d"},
        }
    )
    out = gate.evaluate(raw, {}, _cfg(box, **kwargs))
    assert out is not None, f"expected a deny for {command!r}"
    data = json.loads(out)
    assert data["permissionDecision"] == "deny"
    assert (
        data["hookSpecificOutput"]["permissionDecisionReason"] == data["permissionDecisionReason"]
    )
    return data["permissionDecisionReason"]


PREFLIGHT_FIX = (
    f"Fix: commit all changes, then run from the repo root: {RUNNER_CMD} "
    "(exit 0 = pass; re-run after any new commit), then retry."
)


def assert_self_sufficient(
    text: str, box: Sandbox, why: str, fix: str = PREFLIGHT_FIX
) -> list[str]:
    lines = text.splitlines()
    head7 = box.git("rev-parse", "HEAD")[:7]
    assert len(text) <= gate.REASON_LIMIT
    assert all(ch == "\n" or " " <= ch <= "~" for ch in text)
    assert "|" not in text
    assert lines[0].startswith("preflight gate: push/PR blocked: ")
    assert why in lines[0]
    assert f"(HEAD {head7})" in lines[0]
    assert lines[1] == fix
    if fix != PREFLIGHT_FIX and "--base" not in fix:
        assert "preflight.py" not in fix
    assert "draft" not in text.lower()
    assert lines[-1] == (
        "Bypass is for humans only (APM_PREFLIGHT_BYPASS=accept-unverified when launching the CLI)."
    )
    return lines


def _preflight(box: Sandbox, fake_policy, *argv: str) -> int:
    return preflight.run(list(argv), policy=fake_policy, cwd=box.root)


def test_missing_receipt(sandbox: Sandbox) -> None:
    lines = assert_self_sufficient(
        _deny(sandbox, "git push"), sandbox, "no fresh green preflight receipt"
    )
    assert len(lines) == 3


def test_failed_receipt_lists_ids_and_log_paths(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("FAIL_T0", "1\n")
    sandbox.commit()
    assert _preflight(sandbox, fake_policy) == 1
    lines = assert_self_sufficient(
        _deny(sandbox, "gh pr create --fill"), sandbox, "last preflight run failed"
    )
    logs = lib.preflight_dir(sandbox.git_dir) / "logs"
    assert lines[2] == f"Failed checks: t0.flag (log: {logs / 't0.flag.log'})"
    assert (logs / "t0.flag.log").is_file()


def test_partial_receipt(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert _preflight(sandbox, fake_policy, "--tiers", "T0") == 1
    assert_self_sufficient(_deny(sandbox, "git push"), sandbox, "required tier(s) not run: T1")


def test_dirty_receipt_is_ineligible(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("notes.txt", "dirty\n")
    assert _preflight(sandbox, fake_policy) == 1
    assert_self_sufficient(_deny(sandbox, "git push"), sandbox, "not gate-eligible")


def test_stale_commit(sandbox: Sandbox, fake_policy) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    sandbox.write("docs.md", "new\n")
    sandbox.commit()
    assert_self_sufficient(
        _deny(sandbox, "git push"), sandbox, "is not the preflight-verified commit"
    )


def test_bundle_changed(sandbox: Sandbox, fake_policy) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    (sandbox.root / lib.DEPLOYED_SCRIPTS_REL / "marker.txt").write_text("v2\n", encoding="utf-8")
    assert_self_sufficient(_deny(sandbox, "git push"), sandbox, "bundle changed")


def test_base_moved(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    other = workdir / "other"
    run_git(workdir, "clone", "-q", str(sandbox.remote), str(other))
    (other / "u.txt").write_text("u\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "upstream")
    run_git(other, "push", "-q", "origin", "main")
    sandbox.git("fetch", "-q", "origin")
    assert_self_sufficient(_deny(sandbox, "git push"), sandbox, "moved since preflight")


def test_too_old(sandbox: Sandbox, fake_policy) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    assert_self_sufficient(_deny(sandbox, "git push", now=lambda: later), sandbox, "older than 24h")


@pytest.mark.parametrize(
    ("command", "why", "fix"),
    [
        ("git push --tags", "--tags", gate.FIX_PUSH_HEAD),
        ("git push --all origin", "--all", gate.FIX_PUSH_HEAD),
        ("git push origin 'refs/heads/*:refs/heads/*'", "wildcard", gate.FIX_PUSH_HEAD),
        ("git push origin refs/tags/v1", "tag push", gate.FIX_PUSH_HEAD),
        ("cd - && git push", "cannot resolve the target repository", gate.FIX_TARGET),
        ("git push 'unterminated", "could not parse", gate.FIX_PARSE),
        ("gh pr create --base release", "no fresh green preflight receipt", PREFLIGHT_FIX),
    ],
)
def test_unbindable_and_ambiguous(sandbox: Sandbox, command: str, why: str, fix: str) -> None:
    assert_self_sufficient(_deny(sandbox, command), sandbox, why, fix)


def test_tag_and_other_branch_with_receipt_do_not_say_run_preflight(
    sandbox: Sandbox, fake_policy
) -> None:
    sandbox.git("branch", "other", "main")
    sandbox.write("docs.md", "work\n")
    sandbox.commit()
    sandbox.git("tag", "v9.9.9")
    assert _preflight(sandbox, fake_policy) == 0
    assert_self_sufficient(
        _deny(sandbox, "git push origin v9.9.9"), sandbox, "tag 'v9.9.9'", gate.FIX_PUSH_HEAD
    )
    assert_self_sufficient(
        _deny(sandbox, "git push origin other:refs/heads/x"),
        sandbox,
        "push source 'other'",
        gate.FIX_OTHER_SOURCE,
    )
    assert_self_sufficient(
        _deny(sandbox, "git push origin nosuch"),
        sandbox,
        "cannot resolve push source",
        gate.FIX_UNRESOLVED_SOURCE,
    )
    assert_self_sufficient(
        _deny(sandbox, "gh pr create -H nosuch"), sandbox, "PR head 'nosuch'", gate.FIX_PR_HEAD
    )


def test_chained_push_names_the_segment(sandbox: Sandbox, fake_policy) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    text = _deny(sandbox, "git commit --allow-empty -m x && git push origin HEAD")
    fix = "Fix: run `git push origin HEAD` alone in its own tool call, after preflight passes for the final HEAD."
    lines = assert_self_sufficient(
        text,
        sandbox,
        "run the push/PR command as its own tool call after preflight; this call runs "
        "'git commit --allow-empty -m x' before it",
        fix,
    )
    assert len(lines) == 3


def test_pr_base_mismatch_with_receipt(sandbox: Sandbox, fake_policy) -> None:
    assert _preflight(sandbox, fake_policy) == 0
    assert_self_sufficient(
        _deny(sandbox, "gh pr create --base release"),
        sandbox,
        "PR base 'release'",
        f"Fix: target `main`, or re-run preflight against that base: {RUNNER_CMD} "
        "--base origin/release",
    )


def test_internal_error(sandbox: Sandbox, monkeypatch) -> None:
    def boom(_git_dir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(lib, "read_receipt", boom)
    assert_self_sufficient(_deny(sandbox, "git push"), sandbox, "internal error")


def test_long_failure_lists_are_truncated(sandbox: Sandbox) -> None:
    logs = "/Users/someone/src/repo/.git/worktrees/branch-name/apm-preflight/logs"
    failed = [(f"check.{i:03d}", f"{logs}/check.{i:03d}.log") for i in range(80)]
    text = gate.compose_reason("the last preflight run failed", "abcdef1", RUNNER_CMD, failed)
    assert len(text) <= gate.REASON_LIMIT
    assert f"check.000 (log: {logs}/check.000.log)" in text
    assert "more)" in text
    assert text.endswith("launching the CLI).")
