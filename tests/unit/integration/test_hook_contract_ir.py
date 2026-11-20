"""Owner tests for the neutral hook IR (neutral-hook-contract).

Exact ``HookDocument`` equality at ``apm_cli.hook_contract`` so the event,
platform, timeout, provenance and metadata of every handler shape are pinned
(see tests/mutation/floors.json).
"""

from __future__ import annotations

import pytest

from apm_cli.hook_contract import (
    HookBinding,
    HookDocument,
    HookHandler,
    _entries_to_ir,
    _escape_json_pointer,
    _handler_to_ir,
)

pytestmark = pytest.mark.unit


def test_nested_handlers_keep_platform_timeout_and_inherited_provenance() -> None:
    entries = [
        "not-a-dict",
        {
            "matcher": "Bash",
            "_apm_source": "pkg/a",
            "extra": 1,
            "hooks": [
                {"command": "c", "timeoutSec": 5, "bash": "ignored"},
                {"bash": "b"},
                {"powershell": "p", "_apm_source": "own"},
                {"windows": "w", "timeout": 3},
                "skipped",
            ],
        },
    ]

    assert _entries_to_ir(entries, "PreToolUse") == HookDocument(
        bindings=(
            HookBinding(event="PreToolUse", handlers=(), metadata={"raw_entry": "not-a-dict"}),
            HookBinding(
                event="PreToolUse",
                handlers=(
                    HookHandler("c", "all", 5, "pkg/a", {"bash": "ignored"}),
                    HookHandler("b", "posix", None, "pkg/a", {}),
                    HookHandler("p", "windows", None, "own", {}),
                    HookHandler("w", "windows", 3, "pkg/a", {}),
                ),
                matcher="Bash",
                provenance="pkg/a",
                metadata={"extra": 1},
            ),
        )
    )


def test_flat_entry_becomes_one_handler_binding() -> None:
    entries = [{"command": "x", "_apm_source": "pkg/b", "matcher": "m", "k": "v"}]

    assert _entries_to_ir(entries, "Stop") == HookDocument(
        bindings=(
            HookBinding(
                event="Stop",
                handlers=(HookHandler("x", "all", None, "pkg/b", {"k": "v"}),),
                matcher="m",
                provenance="pkg/b",
            ),
        )
    )


def test_default_event_is_empty() -> None:
    assert _entries_to_ir([7]).bindings[0].event == ""


@pytest.mark.parametrize(
    ("raw", "inherited", "expected"),
    [
        ({"command": "c", "_apm_source": "own"}, None, "own"),
        ({"command": "c", "_apm_source": "own"}, "parent", "own"),
        ({"command": "c"}, "parent", "parent"),
        ({"command": "c"}, None, None),
    ],
)
def test_handler_provenance_prefers_its_own_source(raw, inherited, expected) -> None:
    assert _handler_to_ir(raw, inherited).provenance == expected


def test_json_pointer_segments_escape_tilde_before_slash() -> None:
    assert _escape_json_pointer("a~b/c~1") == "a~0b~1c~01"
