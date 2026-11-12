"""Launcher (R1): must never brick tool calls; fast path skips Python."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import gate
import pytest
from conftest import SCRIPTS, Sandbox

LAUNCHER = "gate-launcher.sh"
HOOK_JSON = SCRIPTS.parent / "preflight-gate.json"


def install_bundle(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for path in SCRIPTS.iterdir():
        if path.is_file():
            shutil.copy2(path, dest / path.name)
    return dest


def launch(bundle: Path, stdin: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(bundle / LAUNCHER)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def payload(command: str, cwd: Path) -> str:
    return json.dumps(
        {
            "sessionId": "s",
            "timestamp": 1,
            "cwd": str(cwd),
            "toolName": "bash",
            "toolArgs": {"command": command, "description": "d"},
        }
    )


def test_hook_json_shape() -> None:
    data = json.loads(HOOK_JSON.read_text(encoding="utf-8"))
    assert data["version"] == 1
    (entry,) = data["hooks"]["preToolUse"]
    assert entry["type"] == "command"
    assert isinstance(entry["matcher"], str)
    assert entry["bash"].endswith("|| true; exit 0")
    assert "git rev-parse --show-toplevel" in entry["bash"]
    assert "${PLUGIN_ROOT}/.apm/hooks/scripts/gate-launcher.sh" in entry["bash"]
    assert entry["powershell"] == "exit 0"
    assert entry["timeoutSec"] == 15


def _matcher() -> re.Pattern[str]:
    entry = json.loads(HOOK_JSON.read_text(encoding="utf-8"))["hooks"]["preToolUse"][0]
    # Copilot compiles the matcher as ^(?:PATTERN)$ against toolName.
    return re.compile(f"^(?:{entry['matcher']})$")


PR_TOOL_NAMES = (
    "create_pull_request",
    "github-mcp-server-create_pull_request",
    "mcp_github_create_pull_request",
)


@pytest.mark.parametrize("name", [*gate.SHELL_TOOL_NAMES, *PR_TOOL_NAMES])
def test_matcher_covers_every_gated_tool_name(name: str) -> None:
    assert _matcher().match(name), name
    args = {"input" if name == "write_bash" else "command": "git push"}
    assert gate.classify(name, args, "/repo"), name


@pytest.mark.parametrize("name", ["update_pull_request", "github-mcp-server-update_pull_request"])
def test_matcher_covers_update_pull_request(name: str) -> None:
    assert _matcher().match(name)
    assert gate.classify(name, {"draft": False}, "/repo")
    assert gate.classify(name, {"title": "t", "body": "b"}, "/repo") == []


def test_matcher_covers_gate_shell_set_exactly() -> None:
    assert {n.lower() for n in gate.SHELL_TOOL_NAMES} == gate.SHELL_TOOLS
    assert gate.PR_TOOL_SUFFIX == "create_pull_request"


@pytest.mark.parametrize(
    "name",
    [
        "view",
        "edit",
        "grep",
        "glob",
        "create",
        "read_bash",
        "stop_bash",
        "list_bash",
        "task",
        "web_fetch",
        "sql",
        "bash2",
        "mybash",
        "create_pull_request_review",
        "update_pull_request_branch",
    ],
)
def test_matcher_skips_non_gated_tools(name: str) -> None:
    assert not _matcher().match(name), name
    assert gate.classify(name, {"command": "git push"}, "/repo") == []


def _stub_bundle(dest: Path) -> tuple[Path, Path]:
    """Bundle whose gate.py only records that Python was started."""
    bundle = install_bundle(dest)
    marker = dest / "python-invocations"
    (bundle / "gate.py").write_text(f"open({str(marker)!r}, 'a').write('x')\n", encoding="utf-8")
    return bundle, marker


def _tool_payload(tool: str, args: dict) -> str:
    return json.dumps(
        {"sessionId": "s", "timestamp": 1, "cwd": "/repo", "toolName": tool, "toolArgs": args}
    )


NON_PUBLISH = [
    "ls -la",
    "cat README.md",
    "git status",
    "git diff --stat",
    "git commit -m 'fix tests'",
    "git log --oneline -5",
    "git fetch origin",
    "git pull --rebase",
    "gh issue create --title x --body y",
    "gh issue list",
    "gh run view 123",
    "gh api repos/o/r/issues",
    "uv run pytest tests/unit -q",
    "python3 preflight.py --plan",
]

PUBLISH = [
    ("bash", {"command": "git push origin HEAD"}),
    ("bash", {"command": "cd sub && git -C . push"}),
    ("bash", {"command": "gh pr create --fill"}),
    ("bash", {"command": "gh pr ready 12"}),
    ("bash", {"command": "gh api -X POST repos/o/r/pulls -f title=t"}),
    ("bash", {"command": "gh api graphql -f query='mutation { createPullRequest }'"}),
    ("write_bash", {"input": "git push\n"}),
    ("create_pull_request", {"title": "t", "base_branch": "main"}),
    ("github-mcp-server-create_pull_request", {"owner": "o", "repo": "r"}),
    ("update_pull_request", {"pullNumber": 3, "draft": False}),
]


@pytest.mark.parametrize("command", NON_PUBLISH)
def test_non_publish_shell_commands_never_start_python(workdir: Path, command: str) -> None:
    bundle, marker = _stub_bundle(workdir / "bundle")
    proc = launch(bundle, _tool_payload("bash", {"command": command, "description": "d"}))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not marker.exists(), command


@pytest.mark.parametrize(("tool", "args"), PUBLISH)
def test_publish_payloads_start_python(workdir: Path, tool: str, args: dict) -> None:
    bundle, marker = _stub_bundle(workdir / "bundle")
    proc = launch(bundle, _tool_payload(tool, args))
    assert proc.returncode == 0
    assert marker.read_text(encoding="utf-8") == "x"


def test_update_pull_request_text_edits_never_start_python(workdir: Path) -> None:
    bundle, marker = _stub_bundle(workdir / "bundle")
    launch(bundle, _tool_payload("update_pull_request", {"title": "t", "body": "b"}))
    assert not marker.exists()


def test_string_tool_args_publish_is_detected(workdir: Path) -> None:
    bundle, marker = _stub_bundle(workdir / "bundle")
    raw = json.dumps({"cwd": "/repo", "toolName": "bash", "toolArgs": '{"command":"git push"}'})
    launch(bundle, raw)
    assert marker.exists()


def test_gated_call_without_receipt_is_denied_through_launcher(sandbox: Sandbox) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    proc = launch(bundle, payload("git push origin feature", sandbox.root))
    assert proc.returncode == 0
    (line,) = proc.stdout.splitlines()
    decision = json.loads(line)
    assert decision["permissionDecision"] == "deny"
    assert "no fresh green preflight receipt" in decision["permissionDecisionReason"]
    assert not (bundle / "__pycache__").exists()


@pytest.mark.parametrize("breakage", ["missing", "syntax", "crash"])
def test_broken_gate_fails_open(sandbox: Sandbox, breakage: str) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    gate = bundle / "gate.py"
    if breakage == "missing":
        gate.unlink()
    elif breakage == "syntax":
        gate.write_text("def broken(:\n", encoding="utf-8")
    else:
        gate.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    proc = launch(bundle, payload("git push", sandbox.root))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_python_missing_fails_open(sandbox: Sandbox, workdir: Path) -> None:
    bundle = install_bundle(sandbox.root / "bundle")
    bin_dir = workdir / "bin"
    bin_dir.mkdir()
    for tool in ("cat", "dirname"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    env = {"PATH": str(bin_dir), "HOME": os.environ.get("HOME", "")}
    proc = launch(bundle, payload("git push", sandbox.root), env=env)
    assert (proc.returncode, proc.stdout) == (0, "")


def test_missing_launcher_fails_open_via_hook_command(workdir: Path) -> None:
    command = json.loads(HOOK_JSON.read_text(encoding="utf-8"))["hooks"]["preToolUse"][0]["bash"]
    resolved = command.replace("${PLUGIN_ROOT}", str(workdir / "nowhere"))
    proc = subprocess.run(
        ["bash", "-c", resolved],
        input=payload("git push", workdir),
        capture_output=True,
        text=True,
        check=False,
        cwd=workdir,
    )
    assert (proc.returncode, proc.stdout) == (0, "")


def _deployed_command(prefix: str) -> str:
    command = json.loads(HOOK_JSON.read_text(encoding="utf-8"))["hooks"]["preToolUse"][0]["bash"]
    return command.replace("${PLUGIN_ROOT}", prefix)


@pytest.mark.parametrize("subdir", ["", "docs", "docs/deep/er"])
def test_project_hook_command_works_from_subdirectories(sandbox: Sandbox, subdir: str) -> None:
    """D2: the repo-relative deployed command must resolve from any cwd."""
    install_bundle(sandbox.root / ".github/hooks/scripts/preflight-gate/.apm/hooks/scripts")
    cwd = sandbox.root / subdir
    cwd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", "-c", _deployed_command(".github/hooks/scripts/preflight-gate")],
        input=payload("git push origin HEAD", cwd),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    assert proc.returncode == 0
    (line,) = proc.stdout.splitlines()
    assert json.loads(line)["permissionDecision"] == "deny"


def test_user_scope_absolute_hook_command(sandbox: Sandbox, workdir: Path) -> None:
    bundle = install_bundle(workdir / "user-hooks" / ".apm/hooks/scripts")
    proc = subprocess.run(
        ["bash", "-c", _deployed_command(str(bundle.parent.parent.parent))],
        input=payload("git push origin HEAD", sandbox.root / "README.md").replace("/README.md", ""),
        capture_output=True,
        text=True,
        check=False,
        cwd=workdir,
    )
    assert proc.returncode == 0
