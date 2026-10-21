"""Selected native hook bindings retain invocation semantics or fail closed."""

import copy

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_native_formats import render_selected_hook_document

pytestmark = pytest.mark.unit


def test_copilot_native_aliases_keep_distinct_payload_and_matcher_contracts() -> None:
    source = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo ok"}]}
            ],
            "preToolUse": [{"type": "command", "command": "echo ok", "matcher": "bash"}],
            "SubagentStop": [{"type": "command", "command": "echo done"}],
            "subagentStop": [{"type": "command", "command": "echo done"}],
        }
    }
    rendered = render_selected_hook_document(source, "copilot")
    assert set(rendered["hooks"]) == set(source["hooks"])
    assert rendered["hooks"]["PreToolUse"] == [
        {"type": "command", "command": "echo ok", "matcher": "Bash"}
    ]
    assert rendered["hooks"]["preToolUse"] == source["hooks"]["preToolUse"]


@pytest.mark.parametrize(
    "target,event",
    [
        ("claude", "subagentStop"),
        ("claude", "preToolUse"),
        ("claude", "InventedEvent"),
        ("copilot", "InventedEvent"),
    ],
)
def test_unsupported_event_is_not_passed_through_or_guessed(target: str, event: str) -> None:
    with pytest.raises(HookContractError, match=r"unsupported.*event"):
        render_selected_hook_document(
            {"hooks": {event: [{"type": "command", "command": "echo ok"}]}}, target
        )


@pytest.mark.parametrize("matcher", ["Bash", "*", ""])
def test_claude_preserves_matcher_and_handler_invocation(matcher: str) -> None:
    handler = {
        "type": "command",
        "command": "echo",
        "args": ["ok"],
        "if": "Bash(git *)",
        "timeout": 15,
        "async": False,
        "shell": "bash",
    }
    source = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [handler]}]}}
    original = copy.deepcopy(source)
    rendered = render_selected_hook_document(source, "claude")
    assert rendered["hooks"]["PreToolUse"][0]["hooks"] == [handler]
    assert rendered["hooks"]["PreToolUse"][0]["matcher"] == (matcher or "*")
    assert source == original


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "command", "bash": "echo posix"},
        {"type": "command", "command": "echo ok", "powershell": "echo windows"},
        {"type": "command", "command": "echo ok", "env": {"A": "B"}},
        {"type": "command", "command": "echo ok", "once": True},
        {"type": "command", "command": "node", "args": ["./hooks/check.js"]},
        {"type": "command", "command": "echo ok", "timeout": -1},
    ],
)
def test_unrepresentable_claude_conditions_are_not_discarded(handler: dict) -> None:
    with pytest.raises(HookContractError):
        render_selected_hook_document({"hooks": {"PreToolUse": [handler]}}, "claude")


def test_copilot_does_not_drop_inner_or_outer_matcher() -> None:
    source = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo ok", "matcher": "Edit"}],
                }
            ]
        }
    }
    with pytest.raises(HookContractError, match=r"combine.*matchers"):
        render_selected_hook_document(source, "copilot")


def test_nested_claude_handler_requires_its_native_type() -> None:
    with pytest.raises(HookContractError, match="explicit type"):
        render_selected_hook_document(
            {"hooks": {"PreToolUse": [{"hooks": [{"command": "echo ok"}]}]}}, "claude"
        )


def test_copilot_preserves_platform_and_environment_fields() -> None:
    handler = {
        "type": "command",
        "bash": "echo posix",
        "powershell": "echo windows",
        "cwd": ".",
        "env": {"FLAG": "on"},
        "timeoutSec": 7,
    }
    source = {"hooks": {"sessionStart": [handler]}}
    assert render_selected_hook_document(source, "copilot")["hooks"] == source["hooks"]


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_malformed_source_is_not_silently_omitted(target: str) -> None:
    with pytest.raises(HookContractError):
        render_selected_hook_document({"hooks": {"PreToolUse": [False]}}, target)


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_malformed_http_url_reports_a_native_contract_error(target: str) -> None:
    with pytest.raises(HookContractError, match="HTTP hook URL"):
        render_selected_hook_document(
            {"hooks": {"PreToolUse": [{"type": "http", "url": "https://[invalid"}]}},
            target,
        )


@pytest.mark.parametrize(
    "event,handler",
    [
        ("SessionStart", {"type": "prompt", "prompt": "check"}),
        ("Notification", {"type": "agent", "prompt": "check"}),
        ("PermissionRequest", {"type": "agent", "prompt": "check"}),
        ("SessionStart", {"type": "http", "url": "https://example.com/hook"}),
        ("Setup", {"type": "mcp_tool", "server": "local", "tool": "check"}),
        ("PreToolUse", {"type": "mcp_tool", "server": "local"}),
        ("PreToolUse", {"type": "mcp_tool", "server": "local", "tool": "check", "input": []}),
        ("PreToolUse", {"command": "echo ok", "onFailure": []}),
        ("PreToolUse", {"command": "echo ok", "args": []}),
    ],
)
def test_unsupported_handler_event_combinations_fail(event: str, handler: dict) -> None:
    with pytest.raises(HookContractError):
        render_selected_hook_document({"hooks": {event: [handler]}}, "claude")


@pytest.mark.parametrize(
    "handler",
    [
        {
            "type": "mcp_tool",
            "server": "local",
            "tool": "check",
            "input": {"file": "${tool_input.path}"},
        },
        {"type": "http", "url": "https://example.com/hook", "onFailure": "block"},
        {"type": "prompt", "prompt": "check", "model": "configured-model"},
        {"type": "agent", "prompt": "check", "timeout": 45},
    ],
)
def test_supported_noncommand_handlers_preserve_payload(handler: dict) -> None:
    result = render_selected_hook_document({"hooks": {"PreToolUse": [handler]}}, "claude")
    assert result["hooks"]["PreToolUse"][0]["hooks"] == [handler]


@pytest.mark.parametrize("placement", ["settings", "skill", "agent"])
def test_once_follows_native_primitive_lifetime(placement: str) -> None:
    source = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo ok", "once": True}],
                }
            ]
        }
    }
    original = copy.deepcopy(source)
    if placement == "skill":
        assert render_selected_hook_document(source, "claude", placement=placement) == source
    else:
        with pytest.raises(HookContractError, match="once"):
            render_selected_hook_document(source, "claude", placement=placement)
    assert source == original
