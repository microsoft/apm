"""Classification matrix: which tool calls are gated (R2 parse-don't-regex)."""

from __future__ import annotations

import gate
import pytest

CWD = "/work/repo"


def kinds(command: str, tool: str = "bash") -> list[str]:
    return [a.kind for a in gate.classify(tool, {"command": command}, CWD)]


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin feature",
        "git push -u origin feature",
        "git push --force-with-lease origin HEAD",
        "git --no-pager push",
        "git -C /elsewhere --no-pager push",
        "git -c push.default=simple push",
        "/usr/bin/git push",
        "FOO=1 git push",
        "env FOO=1 git push",
        "command git push",
        "time git push",
        "nohup git push &",
        "sudo -u bob git push",
        "xargs -n1 git push",
        "timeout 60 git push",
        "ls && git push",
        "ls; git push",
        "false || git push",
        "ls | git push",
        "(git push)",
        "true\ngit push",
        "if git push; then echo ok; fi",
        "echo $(git push)",
        'echo "$(git push)"',
        "echo `git push`",
        "bash -c 'git push origin main'",
        'sh -c "cd sub && git push"',
        "bash -lc 'git push'",
        "eval git push",
        "git push --dry-run && git push",
        "git push 2>&1 | tail -5",
        "git push > out.log 2>&1",
    ],
)
def test_git_push_shapes_are_gated(command: str) -> None:
    assert "git-push" in kinds(command)


@pytest.mark.parametrize(
    "command",
    [
        'echo "git push"',
        "grep 'gh pr create' notes.txt",
        "git status",
        "git pull",
        "git log --grep push",
        "git commit -m 'git push later'",
        "git push --dry-run",
        "git push -n origin feature",
        "git push origin --delete old",
        "git push -d origin old",
        "git push origin :old",
        "cat push.txt",
        "ls -la",
        "command -v git",
        "git xyz",
        "printf 'gh pr create\\n'",
    ],
)
def test_not_gated(command: str) -> None:
    assert kinds(command) == []


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("gh pr create --fill", "gh-pr-create"),
        ("gh pr new --title x", "gh-pr-create"),
        ("gh -R owner/repo pr create", "gh-pr-create"),
        ("gh pr create -R owner/repo --base main", "gh-pr-create"),
        ("gh pr ready 12", "gh-pr-ready"),
        ("gh api -X POST repos/o/r/pulls", "gh-api-pulls"),
        ("gh api repos/o/r/pulls -f title=x -f head=b -f base=main", "gh-api-pulls"),
        ("gh api --method=PATCH repos/o/r/pulls/3", "gh-api-pulls"),
        (
            "gh api graphql -f query='mutation { markPullRequestReadyForReview(input: {}) { x } }'",
            "gh-api-pulls",
        ),
    ],
)
def test_gh_shapes_are_gated(command: str, kind: str) -> None:
    assert kinds(command) == [kind]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr ready 3 --undo",
        "gh pr view 3",
        "gh pr list",
        "gh api repos/o/r/pulls",
        "gh api repos/o/r/pulls/1/comments -f body=x",
        "gh issue create --title x",
    ],
)
def test_gh_not_gated(command: str) -> None:
    assert kinds(command) == []


def test_git_aliases_are_not_expanded(monkeypatch) -> None:
    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("classification must not run git")

    monkeypatch.setattr(gate.lib, "git", no_subprocess)
    monkeypatch.setattr(gate.lib, "git_out", no_subprocess)
    assert kinds("git p") == []
    assert kinds("git sync && git st") == []
    assert kinds("git p && git push") == ["git-push"]


@pytest.mark.parametrize(
    ("command", "reason_part"),
    [
        ("git push --tags", "--tags"),
        ("git push --all origin", "--all"),
        ("git push --mirror origin", "--mirror"),
        ("git push --follow-tags", "--follow-tags"),
        ("git push origin 'refs/heads/*:refs/heads/*'", "wildcard"),
        ("git push origin refs/tags/v1.0", "tag push"),
        ("git push origin main:refs/tags/v1", "tag push"),
        ("git push origin tag v1.0", "tag push"),
    ],
)
def test_unbindable_pushes_carry_a_deny(command: str, reason_part: str) -> None:
    actions = gate.classify("bash", {"command": command}, CWD)
    assert len(actions) == 1
    assert reason_part in (actions[0].deny or "")


def test_push_sources_are_extracted() -> None:
    (action,) = gate.classify(
        "bash", {"command": "git push origin +a:refs/heads/x b :gone HEAD"}, CWD
    )
    assert action.sources == ["a", "b", "HEAD"]
    (default,) = gate.classify("bash", {"command": "git push"}, CWD)
    assert default.sources == ["HEAD"]


def test_dry_run_exemption_is_per_simple_command() -> None:
    actions = gate.classify("bash", {"command": "git push --dry-run && git push origin x"}, CWD)
    assert [a.sources for a in actions] == [["x"]]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push", CWD),
        ("cd /a && git push", "/a"),
        ("cd sub && git push", CWD + "/sub"),
        ("pushd /a && git push", "/a"),
        ("git -C /b push", "/b"),
        ("git -C rel push", CWD + "/rel"),
        ("cd /a && git -C ../b push", "/b"),
        ("cd - && git push", None),
        ("cd $UNSET_PREFLIGHT_VAR && git push", None),
        ("git --git-dir=/x/.git push", None),
        ("env -C /c git push", "/c"),
    ],
)
def test_target_directory_tracking(command: str, expected: str | None) -> None:
    (action,) = gate.classify("bash", {"command": command}, CWD)
    assert action.cwd == expected


def test_subshell_cd_does_not_leak() -> None:
    actions = gate.classify("bash", {"command": "(cd /a && git push); git push"}, CWD)
    assert [a.cwd for a in actions] == ["/a", CWD]


def test_unparseable_gated_text_is_conservatively_gated() -> None:
    (action,) = gate.classify("bash", {"command": "git push 'unterminated"}, CWD)
    assert action.kind == "unparseable"
    assert action.deny
    assert gate.classify("bash", {"command": "echo 'unterminated"}, CWD) == []


@pytest.mark.parametrize("tool", ["bash", "Bash", "shell", "powershell", "write_bash"])
def test_shell_tool_names(tool: str) -> None:
    key = "input" if tool == "write_bash" else "command"
    assert [a.kind for a in gate.classify(tool, {key: "git push"}, CWD)] == ["git-push"]


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("create_pull_request", {"title": "x", "base_branch": "main"}, ["tool-create-pr"]),
        ("github-mcp-server-create_pull_request", {"base": "dev"}, ["tool-create-pr"]),
        ("view", {"path": "git push"}, []),
        ("edit", {"command": "git push"}, []),
        ("bash", {"description": "no command"}, []),
    ],
)
def test_non_shell_tools(tool: str, args: dict, expected: list[str]) -> None:
    assert [a.kind for a in gate.classify(tool, args, CWD)] == expected


def test_create_pr_tool_carries_base() -> None:
    (action,) = gate.classify("create_pull_request", {"base_branch": "dev"}, CWD)
    assert action.base == "dev"


def test_gh_pr_create_flags() -> None:
    (action,) = gate.classify("bash", {"command": "gh pr create -B release --head me:topic"}, CWD)
    assert (action.base, action.head) == ("release", "me:topic")


@pytest.mark.parametrize("command", ["gh pr create --help", "gh pr create -h", "gh pr new --help"])
def test_gh_pr_create_help_is_exempt(command: str) -> None:
    assert kinds(command) == []


@pytest.mark.parametrize(
    ("args", "gated"),
    [
        ({"pullNumber": 1, "draft": False}, True),
        ({"pullNumber": 1, "draft": "false"}, True),
        ({"pullNumber": 1, "ready_for_review": True}, True),
        ({"pullNumber": 1, "draft": True}, False),
        ({"pullNumber": 1, "title": "new title", "body": "text"}, False),
        ({"pullNumber": 1, "state": "closed"}, False),
    ],
)
def test_update_pull_request_only_gates_ready(args: dict, gated: bool) -> None:
    actions = gate.classify("update_pull_request", args, CWD)
    assert [a.kind for a in actions] == (["tool-pr-ready"] if gated else [])
