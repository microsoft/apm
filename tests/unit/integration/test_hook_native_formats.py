"""Native hook rendering materializes frozen IR metadata (#3167)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from apm_cli.hook_contract import _entries_to_ir
from apm_cli.integration.hook_native_formats import (
    _render_nested_document,
    _to_antigravity_hook_entries,
    _to_claude_hook_entries,
    _to_codex_hook_entries,
    _to_gemini_hook_entries,
)

pytestmark = pytest.mark.unit

_ENV = {"APM_TEST": "value", "NESTED": {"LEVEL": ["a", {"b": 1}]}}
_ARGS = ["--flag", {"key": [1, 2]}]


def _nested_entries() -> list[dict[str, Any]]:
    return [
        {
            "matcher": "Bash",
            "_apm_source": "pkg",
            "labels": {"team": ["core"]},
            "hooks": [
                {
                    "type": "command",
                    "command": "./scripts/check.sh",
                    "timeout": 5,
                    "env": dict(_ENV),
                    "args": list(_ARGS),
                }
            ],
        }
    ]


def _flat_entries() -> list[dict[str, Any]]:
    return [{"bash": "echo test", "env": {"APM_TEST": "value"}, "args": list(_ARGS)}]


def _assert_plain_json(value: object) -> None:
    """Every container is a builtin dict or list, so json.dumps needs no default."""
    if isinstance(value, dict):
        for item in value.values():
            _assert_plain_json(item)
    elif isinstance(value, list):
        for item in value:
            _assert_plain_json(item)
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), type(value)


def test_issue_reproduction_serializes() -> None:
    rendered = _to_claude_hook_entries([{"bash": "echo test", "env": {"APM_TEST": "value"}}])

    assert json.loads(json.dumps(rendered)) == [
        {"matcher": "*", "hooks": [{"env": {"APM_TEST": "value"}, "command": "echo test"}]}
    ]
    _assert_plain_json(rendered)


@pytest.mark.parametrize(
    ("render", "matcher", "timeout"),
    [
        (_to_claude_hook_entries, "Bash", 5),
        (_to_codex_hook_entries, "Bash", 5),
        (_to_gemini_hook_entries, "Bash", 5000),
        (lambda entries: _to_antigravity_hook_entries(entries, "PreToolUse"), "Bash", 5),
    ],
    ids=["claude", "codex", "gemini", "antigravity-nested"],
)
def test_nested_targets_preserve_metadata(render, matcher, timeout) -> None:
    rendered = render(_nested_entries())

    _assert_plain_json(rendered)
    assert json.loads(json.dumps(rendered)) == [
        {
            "labels": {"team": ["core"]},
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "env": _ENV,
                    "args": _ARGS,
                    "command": "./scripts/check.sh",
                    "timeout": timeout,
                }
            ],
            "_apm_source": "pkg",
        }
    ]


def test_antigravity_flat_preserves_metadata_and_provenance() -> None:
    entries = [{"_apm_source": "pkg", **_flat_entries()[0], "timeout": 3}]
    rendered = _to_antigravity_hook_entries(entries, "SessionStart")

    _assert_plain_json(rendered)
    assert rendered == [
        {
            "env": {"APM_TEST": "value"},
            "args": _ARGS,
            "command": "echo test",
            "timeout": 3,
            "_apm_source": "pkg",
        }
    ]


@pytest.mark.parametrize(
    "render",
    [
        _to_claude_hook_entries,
        _to_codex_hook_entries,
        _to_gemini_hook_entries,
        lambda entries: _to_antigravity_hook_entries(entries, "PreToolUse"),
        lambda entries: _to_antigravity_hook_entries(entries, "SessionStart"),
    ],
    ids=["claude", "codex", "gemini", "antigravity-nested", "antigravity-flat"],
)
def test_raw_entries_are_materialized(render) -> None:
    raw = [{"env": {"APM_TEST": "value"}}, ["x", {"y": [1]}]]

    rendered = render([raw])

    assert rendered == [raw]
    _assert_plain_json(rendered)


def test_rendered_output_is_independent_of_ir() -> None:
    document = _entries_to_ir([*_nested_entries(), [{"env": {"A": "1"}}]])

    first = _render_nested_document(document, timeout_milliseconds=False)
    first[0]["labels"]["team"].append("mutated")
    first[0]["hooks"][0]["env"]["NESTED"]["LEVEL"][1]["b"] = 2
    first[0]["hooks"][0]["args"].clear()
    first[1][0]["env"]["A"] = "mutated"

    second = _render_nested_document(document, timeout_milliseconds=False)
    assert second[0]["labels"] == {"team": ["core"]}
    assert second[0]["hooks"][0]["env"] == _ENV
    assert second[0]["hooks"][0]["args"] == _ARGS
    assert second[1] == [{"env": {"A": "1"}}]


def test_rendering_does_not_mutate_source_entries() -> None:
    entries = _nested_entries()
    rendered = _to_claude_hook_entries(entries)

    rendered[0]["hooks"][0]["env"]["APM_TEST"] = "mutated"

    assert entries == _nested_entries()
