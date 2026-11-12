"""Gate behavior: payload shapes, receipt states, bindings, bypass, errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gate
import preflight
import preflight_lib as lib
import pytest
from conftest import SCRIPTS, Sandbox, make_sandbox, run_git

REAL_PAYLOAD = {
    "sessionId": "6942355f-0729-4376-bcb7-aeb2dd9e99a2",
    "timestamp": 1794419267296,
    "cwd": "/private/tmp/hookprobe",
    "toolCallId": "toolu_01WWko3wqtHfAHkmpPuHGcCy",
    "toolName": "bash",
    "toolArgs": {"command": "echo PROBE_DENY", "description": "Run probe command"},
}


def payload(box: Sandbox, command: str, *, cwd: Path | None = None) -> str:
    data = dict(REAL_PAYLOAD)
    data["cwd"] = str(cwd or box.root)
    data["toolArgs"] = {"command": command, "description": "test"}
    return json.dumps(data)


def cfg(box: Sandbox, **kwargs) -> gate.GateConfig:
    return gate.GateConfig(scripts_dir=box.root / lib.DEPLOYED_SCRIPTS_REL, **kwargs)


def evaluate(
    box: Sandbox, command: str, env: dict | None = None, lookup=None, **kwargs
) -> dict | None:
    extra = {"pr_lookup": lookup} if lookup else {}
    out = gate.evaluate(payload(box, command, **kwargs), env or {}, cfg(box, **extra))
    return json.loads(out) if out else None


def pr_at(head: str, base: str = "main", seen: list | None = None):
    """Fake PR lookup: every PR has *head* / *base*; records the targets."""

    def lookup(_root: str, target: gate.PRTarget) -> dict:
        if seen is not None:
            seen.append(target)
        return {"headRefOid": head, "baseRefName": base}

    return lookup


def preflight_ok(box: Sandbox, fake_policy, *extra: str) -> int:
    return preflight.run(list(extra), policy=fake_policy, cwd=box.root)


def edit_receipt(box: Sandbox, **changes) -> None:
    data = box.receipt()
    data.update(changes)
    lib.write_json_atomic(lib.receipt_path(box.git_dir), data)


def reason(decision: dict | None) -> str:
    assert decision is not None, "expected a deny"
    assert decision["permissionDecision"] == "deny"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "allow" not in json.dumps(decision)
    return decision["permissionDecisionReason"]


# --- payload shapes ---------------------------------------------------------


def test_real_captured_payload_is_not_gated() -> None:
    assert gate.evaluate(json.dumps(REAL_PAYLOAD), {}) is None


def test_payload_shapes_are_normalized(sandbox: Sandbox) -> None:
    camel = gate.parse_payload(payload(sandbox, "git push"))
    as_string = dict(json.loads(payload(sandbox, "git push")))
    as_string["toolArgs"] = json.dumps(as_string["toolArgs"])
    pascal = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "cwd": str(sandbox.root),
        "tool_name": "Bash",
        "tool_input": {"command": "git push"},
    }
    for raw in (json.dumps(as_string), json.dumps(pascal)):
        name, args, cwd = gate.parse_payload(raw)
        assert name.lower() == "bash"
        assert args["command"] == "git push"
        assert cwd == camel[2]
    for raw in (json.dumps(as_string), json.dumps(pascal)):
        assert "no fresh green preflight receipt" in reason(
            json.loads(gate.evaluate(raw, {}, cfg(sandbox)))
        )


def test_payload_cwd_is_canonicalized(sandbox: Sandbox, workdir: Path) -> None:
    link = workdir / "link"
    link.symlink_to(sandbox.root)
    _, _, cwd = gate.parse_payload(payload(sandbox, "ls", cwd=link))
    assert cwd == str(sandbox.root)
    assert "no fresh green preflight receipt" in reason(evaluate(sandbox, "git push", cwd=link))


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"toolName": 3}'])
def test_garbage_payloads_are_silent(raw: str) -> None:
    assert gate.evaluate(raw, {}) is None


# --- receipt states ----------------------------------------------------------


def test_missing_receipt_denies_with_runner_command(sandbox: Sandbox) -> None:
    text = reason(evaluate(sandbox, "git push origin feature"))
    assert "no fresh green preflight receipt" in text
    assert f"python3 {lib.DEPLOYED_SCRIPTS_REL}/preflight.py" in text
    assert lib.BYPASS_VALUE in text


def test_non_gated_calls_are_silent_even_without_receipt(sandbox: Sandbox) -> None:
    for command in ("ls", "git status", 'echo "git push"', "git push --dry-run"):
        assert evaluate(sandbox, command) is None


def test_fresh_receipt_allows_push_and_pr(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy) == 0
    for command in (
        "git push origin feature",
        "git push -u origin HEAD",
        "git -C . --no-pager push",
        "bash -c 'git push origin feature:refs/heads/feature'",
        "gh pr create --fill",
        "gh pr create --base main --help",
    ):
        assert evaluate(sandbox, command) is None, command
    assert (
        evaluate(sandbox, "gh pr ready 3", lookup=pr_at(sandbox.git("rev-parse", "HEAD"))) is None
    )


def test_new_commit_after_receipt_is_stale(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    sandbox.write("docs.md", "more\n")
    sandbox.commit()
    assert "not the preflight-verified commit" in reason(evaluate(sandbox, "git push"))
    assert "not the preflight-verified commit" in reason(evaluate(sandbox, "gh pr create"))


def test_push_of_other_branch_is_denied(sandbox: Sandbox, fake_policy) -> None:
    sandbox.git("branch", "other", "main")
    sandbox.write("docs.md", "more\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy) == 0
    text = reason(evaluate(sandbox, "git push origin other:refs/heads/x"))
    assert "'other'" in text
    assert "cannot resolve" in reason(evaluate(sandbox, "git push origin nosuchref"))


def test_tag_pushes_are_denied_even_with_receipt(sandbox: Sandbox, fake_policy) -> None:
    sandbox.git("tag", "v9.9.9")
    assert preflight_ok(sandbox, fake_policy) == 0
    assert "tag" in reason(evaluate(sandbox, "git push origin v9.9.9"))
    assert "--tags" in reason(evaluate(sandbox, "git push --tags"))


def test_pure_deletions_are_allowed_without_receipt(sandbox: Sandbox) -> None:
    assert evaluate(sandbox, "git push origin --delete old") is None
    assert evaluate(sandbox, "git push origin :old") is None


def test_failed_receipt_lists_checks_and_logs(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("FAIL_T0", "1\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy) == 1
    text = reason(evaluate(sandbox, "git push"))
    assert "last preflight run failed" in text
    assert "Failed checks: t0.flag; logs: " in text
    assert str(lib.preflight_dir(sandbox.git_dir) / "logs") + "/<check-id>.log" in text


def test_partial_receipt_is_denied(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("src/a.py", "x = 1\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy, "--tiers", "T0") == 1
    assert sandbox.receipt()["status"] == "partial"
    assert "'partial'" in reason(evaluate(sandbox, "git push"))


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"schema": 99}, "schema"),
        ({"gate_eligible": False, "ineligible_reasons": ["dirty"]}, "not gate-eligible"),
        ({"bundle_hash": "0" * 64}, "bundle changed"),
        ({"ran_tiers": []}, "missing required tier"),
        ({"status": "fail"}, "status 'fail'"),
        ({"tree": "0" * 40}, "tree does not match"),
        ({"finished_at": "not-a-time"}, "timestamp unreadable"),
    ],
)
def test_tampered_receipt_states(
    sandbox: Sandbox, fake_policy, changes: dict, fragment: str
) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    edit_receipt(sandbox, **changes)
    assert fragment in reason(evaluate(sandbox, "git push"))


def test_failed_check_exit_is_rechecked(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    data = sandbox.receipt()
    data["checks"][0]["exit"] = 1
    lib.write_json_atomic(lib.receipt_path(sandbox.git_dir), data)
    assert "t0.ok" in reason(evaluate(sandbox, "git push"))


def test_corrupt_receipt_is_denied(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    lib.receipt_path(sandbox.git_dir).write_text("{broken", encoding="utf-8")
    assert "unreadable receipt" in reason(evaluate(sandbox, "git push"))


@pytest.mark.parametrize(
    ("delta", "fragment"),
    [(timedelta(hours=25), "older than 24h"), (timedelta(minutes=-10), "in the future")],
)
def test_receipt_age_and_skew(
    sandbox: Sandbox, fake_policy, delta: timedelta, fragment: str
) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    later = datetime.now(timezone.utc) + delta
    out = gate.evaluate(payload(sandbox, "git push"), {}, cfg(sandbox, now=lambda: later))
    assert fragment in reason(json.loads(out))


def test_bundle_change_after_receipt_is_denied(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    (sandbox.root / lib.DEPLOYED_SCRIPTS_REL / "marker.txt").write_text("v2\n", encoding="utf-8")
    assert "bundle changed" in reason(evaluate(sandbox, "git push"))


def test_base_moved_is_denied(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    assert evaluate(sandbox, "git push") is None
    other = workdir / "other"
    run_git(workdir, "clone", "-q", str(sandbox.remote), str(other))
    (other / "upstream.txt").write_text("u\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "upstream")
    run_git(other, "push", "-q", "origin", "main")
    sandbox.git("fetch", "-q", "origin")
    assert "moved since preflight" in reason(evaluate(sandbox, "git push"))


def test_gh_pr_create_base_binding(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("docs.md", "feature work\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy) == 0
    assert evaluate(sandbox, "gh pr create -B main") is None
    assert "PR base 'release'" in reason(evaluate(sandbox, "gh pr create --base release"))
    sandbox.git("branch", "other", "main")
    assert "is not the preflight-verified" in reason(evaluate(sandbox, "gh pr create -H other"))
    assert evaluate(sandbox, "gh pr create -H someone:feature") is None


def test_create_pull_request_tool(sandbox: Sandbox, fake_policy) -> None:
    raw = json.dumps(
        {"toolName": "create_pull_request", "cwd": str(sandbox.root), "toolArgs": {"title": "t"}}
    )
    assert "no fresh green preflight receipt" in reason(
        json.loads(gate.evaluate(raw, {}, cfg(sandbox)))
    )
    assert preflight_ok(sandbox, fake_policy) == 0
    assert gate.evaluate(raw, {}, cfg(sandbox)) is None


# --- implicit pushes under push config ---------------------------------------


def test_implicit_push_with_push_default_matching_is_denied(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    assert evaluate(sandbox, "git push") is None
    assert "push.default=matching" in reason(evaluate(sandbox, "git -c push.default=matching push"))
    sandbox.git("config", "push.default", "matching")
    for command in ("git push", "git push origin", "git push -u origin"):
        text = reason(evaluate(sandbox, command))
        assert "push.default=matching" in text, command
        assert "git push origin HEAD" in text
    assert evaluate(sandbox, "git push origin HEAD") is None
    assert evaluate(sandbox, "git -c Push.Default=simple push") is None


def test_implicit_push_with_remote_push_refspecs_is_denied(sandbox: Sandbox, fake_policy) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    for command in (
        "git -c remote.origin.push=refs/heads/main:refs/heads/main push",
        "git --config-env=push.default=PD push",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=push.default GIT_CONFIG_VALUE_0=matching git push",
        "env GIT_CONFIG_GLOBAL=/x/cfg git push origin",
    ):
        assert reason(evaluate(sandbox, command)), command
    assert evaluate(sandbox, "GIT_CONFIG_COUNT=1 git push origin HEAD") is None
    sandbox.git("config", "remote.origin.push", "refs/heads/*:refs/heads/*")
    assert "remote.<name>.push" in reason(evaluate(sandbox, "git push"))
    assert evaluate(sandbox, "git push origin feature") is None


# --- API PR creation and ready operations ----------------------------------------


def test_api_pr_creation_binds_requested_head_and_base(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("docs.md", "feature work\n")
    sandbox.commit()
    sandbox.git("branch", "other", "main")
    assert preflight_ok(sandbox, fake_policy) == 0
    rest = "gh api repos/o/r/pulls -f title=t"
    assert evaluate(sandbox, f"{rest} -f head=feature -f base=main") is None
    assert evaluate(sandbox, f"{rest} -f head=me:feature -f base=main") is None
    assert "is not the preflight-verified" in reason(
        evaluate(sandbox, f"{rest} -f head=other -f base=main")
    )
    assert "PR base 'release'" in reason(
        evaluate(sandbox, f"{rest} -f head=feature -f base=release")
    )
    assert "cannot establish the head" in reason(evaluate(sandbox, rest))
    assert "cannot establish the head" in reason(evaluate(sandbox, f"{rest} --input body.json"))
    assert "cannot resolve PR head" in reason(evaluate(sandbox, f"{rest} -f head=nosuch"))
    mutation = (
        "gh api graphql -f query='mutation { createPullRequest(input: "
        '{repositoryId: "R", headRefName: "HEADNAME", baseRefName: "main", title: "t"}) '
        "{ pullRequest { url } } }'"
    )
    assert evaluate(sandbox, mutation.replace("HEADNAME", "feature")) is None
    assert "is not the preflight-verified" in reason(
        evaluate(sandbox, mutation.replace("HEADNAME", "other"))
    )
    variables = (
        "gh api graphql -f query='mutation($h: String!) { createPullRequest(input: "
        '{repositoryId: "R", headRefName: $h, baseRefName: "main", title: "t"}) { x } }\' '
    )
    assert evaluate(sandbox, variables + "-f h=feature") is None
    assert "is not the preflight-verified" in reason(evaluate(sandbox, variables + "-f h=other"))
    assert "cannot establish the head" in reason(evaluate(sandbox, variables))


def test_ready_operations_bind_the_prs_actual_head(sandbox: Sandbox, fake_policy) -> None:
    sandbox.write("docs.md", "feature work\n")
    sandbox.commit()
    assert preflight_ok(sandbox, fake_policy) == 0
    verified = sandbox.git("rev-parse", "HEAD")
    stale = sandbox.git("rev-parse", "main")
    seen: list[gate.PRTarget] = []
    ok = pr_at(verified, seen=seen)
    assert evaluate(sandbox, "gh pr ready", lookup=ok) is None
    assert evaluate(sandbox, "gh pr ready 7 -R o/r", lookup=ok) is None
    assert evaluate(sandbox, "gh api -X PATCH repos/o/r/pulls/8 -f state=open", lookup=ok) is None
    node = (
        "gh api graphql -f query='mutation { markPullRequestReadyForReview("
        'input: {pullRequestId: "PR_kw1"}) { x } }\''
    )
    assert evaluate(sandbox, node, lookup=ok) is None
    tool = {"toolName": "update_pull_request", "cwd": str(sandbox.root)}
    tool["toolArgs"] = {"owner": "o", "repo": "r", "pullNumber": 9, "draft": False}
    assert gate.evaluate(json.dumps(tool), {}, cfg(sandbox, pr_lookup=ok)) is None
    assert [(t.ref, t.repo, t.node_id) for t in seen] == [
        (None, None, None),
        ("7", "o/r", None),
        ("8", "o/r", None),
        (None, None, "PR_kw1"),
        ("9", "o/r", None),
    ]
    text = reason(evaluate(sandbox, "gh pr ready 7", lookup=pr_at(stale)))
    assert f"PR 7 head ({stale[:12]}) is not the preflight-verified" in text
    assert "push the verified commit to the PR branch first" in text
    assert "PR base 'release'" in reason(
        evaluate(sandbox, "gh pr ready 7", lookup=pr_at(verified, "release"))
    )
    unknown = reason(evaluate(sandbox, "gh pr ready 7", lookup=lambda _r, _t: None))
    assert "cannot establish the head of PR 7" in unknown
    tool["toolArgs"] = {"draft": False}
    assert "no PR number" in reason(
        json.loads(gate.evaluate(json.dumps(tool), {}, cfg(sandbox, pr_lookup=ok)))
    )


def _fake_gh(workdir: Path, monkeypatch, body: str) -> Path:
    bin_dir = workdir / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def test_default_pr_lookup_asks_gh_for_the_pr_head(
    sandbox: Sandbox, fake_policy, workdir: Path, monkeypatch
) -> None:
    assert preflight_ok(sandbox, fake_policy) == 0
    verified = sandbox.git("rev-parse", "HEAD")
    argv_log = workdir / "gh-argv"
    _fake_gh(
        workdir,
        monkeypatch,
        f'printf "%s\\n" "$@" > {argv_log}\n'
        f'echo \'{{"headRefOid": "{verified}", "baseRefName": "main"}}\'',
    )
    assert evaluate(sandbox, "gh pr ready 7 -R o/r") is None
    assert argv_log.read_text(encoding="utf-8").split() == [
        "pr",
        "view",
        "7",
        "--json",
        "headRefOid,baseRefName",
        "-R",
        "o/r",
    ]


def test_pr_lookup_timeout_denies_within_hook_budget(
    sandbox: Sandbox, fake_policy, workdir: Path, monkeypatch
) -> None:
    assert gate.PR_LOOKUP_TIMEOUT_S < 15
    assert preflight_ok(sandbox, fake_policy) == 0
    _fake_gh(workdir, monkeypatch, "sleep 30")
    monkeypatch.setattr(gate, "PR_LOOKUP_TIMEOUT_S", 0.5)
    began = time.monotonic()
    assert "cannot establish the head of PR 7" in reason(evaluate(sandbox, "gh pr ready 7"))
    assert time.monotonic() - began < 10


# --- bypass, scope, errors ---------------------------------------------------------


def test_bypass_requires_exact_value(sandbox: Sandbox) -> None:
    assert evaluate(sandbox, "git push", env={lib.BYPASS_ENV: lib.BYPASS_VALUE}) is None
    log = lib.preflight_dir(sandbox.git_dir) / lib.BYPASS_LOG_NAME
    assert "git push" in log.read_text(encoding="utf-8")
    for value in ("1", "yes", "true", "accept-unverified "):
        assert reason(evaluate(sandbox, "git push", env={lib.BYPASS_ENV: value}))


def test_bypass_is_not_parsed_from_the_command(sandbox: Sandbox) -> None:
    command = f"{lib.BYPASS_ENV}={lib.BYPASS_VALUE} git push"
    assert "no fresh green preflight receipt" in reason(evaluate(sandbox, command))


def test_other_repositories_are_out_of_scope(sandbox: Sandbox, workdir: Path) -> None:
    (workdir / "second").mkdir()
    other = make_sandbox(workdir / "second")
    assert evaluate(sandbox, f"cd {other.root} && git push") is None
    assert evaluate(sandbox, "git push", cwd=other.root) is None
    assert evaluate(sandbox, f"git -C {other.root} push") is None


def test_unresolvable_target_is_denied(sandbox: Sandbox) -> None:
    assert "cannot resolve the target" in reason(evaluate(sandbox, "cd - && git push"))
    assert "cannot resolve the target" in reason(evaluate(sandbox, "cd /no/such/dir && git push"))


def test_internal_error_after_classification_denies(sandbox: Sandbox, monkeypatch) -> None:
    def boom(_git_dir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(lib, "read_receipt", boom)
    text = reason(evaluate(sandbox, "git push"))
    assert "internal error" in text
    assert lib.BYPASS_ENV in text
    assert evaluate(sandbox, "ls") is None


def test_classification_crash_on_gated_text_denies(sandbox: Sandbox, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(gate, "classify", boom)
    assert "internal error" in reason(evaluate(sandbox, "git push"))
    assert evaluate(sandbox, "ls -la") is None


def test_gate_process_always_exits_zero(sandbox: Sandbox) -> None:
    script = str(SCRIPTS / "gate.py")
    for stdin in ("garbage", "", payload(sandbox, "ls")):
        proc = subprocess.run(
            [sys.executable, "-B", script], input=stdin, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0
        assert proc.stdout == ""


def test_gate_process_prints_single_deny_object(sandbox: Sandbox) -> None:
    deployed = sandbox.root / "hooks"
    deployed.mkdir()
    for name in ("gate.py", "preflight_lib.py"):
        (deployed / name).write_bytes((SCRIPTS / name).read_bytes())
    proc = subprocess.run(
        [sys.executable, "-B", str(deployed / "gate.py")],
        input=payload(sandbox, "git push"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert "no fresh green preflight receipt" in reason(json.loads(lines[0]))
    assert not (deployed / "__pycache__").exists()


def test_linked_worktrees_are_isolated(sandbox: Sandbox, fake_policy, workdir: Path) -> None:
    second = workdir / "wt2"
    sandbox.git("worktree", "add", "-q", "-b", "other", str(second), "feature")
    assert preflight_ok(sandbox, fake_policy) == 0
    second_git_dir = Path(run_git(second, "rev-parse", "--path-format=absolute", "--git-dir"))
    assert second_git_dir != sandbox.git_dir
    assert not lib.receipt_path(second_git_dir).exists()
    assert evaluate(sandbox, "git push") is None
    text = reason(evaluate(sandbox, f"cd {second} && git push"))
    assert "no fresh green preflight receipt" in text
    assert "no fresh green preflight receipt" in reason(evaluate(sandbox, "git push", cwd=second))
