"""Binding, provenance and isolation regressions adapted from #3199."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
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
_NESTED_RENDERERS = [
    _to_claude_hook_entries,
    _to_codex_hook_entries,
    _to_gemini_hook_entries,
    lambda entries: _to_antigravity_hook_entries(entries, "PreToolUse"),
]


def _nested_entries() -> list[dict[str, Any]]:
    """Build independent source metadata with binding provenance."""
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
                    "env": copy.deepcopy(_ENV),
                    "args": copy.deepcopy(_ARGS),
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("render", "timeout"),
    list(zip(_NESTED_RENDERERS, [5, 5, 5000, 5], strict=True)),
    ids=["claude", "codex", "gemini", "antigravity-nested"],
)
def test_nested_targets_preserve_binding_metadata_and_provenance(
    render: Callable[[list], list], timeout: int
) -> None:
    """Nested native groups keep metadata and existing binding provenance."""
    entries = _nested_entries()
    expected = copy.deepcopy(entries)
    expected[0]["hooks"][0]["timeout"] = timeout

    assert json.loads(json.dumps(render(entries))) == expected


def test_antigravity_flat_preserves_metadata_and_provenance() -> None:
    """Flat Antigravity output retains arrays and ownership without a wrapper."""
    rendered = _to_antigravity_hook_entries(
        [{"_apm_source": "pkg", "bash": "echo test", "env": _ENV, "args": _ARGS, "timeout": 3}],
        "SessionStart",
    )

    assert json.loads(json.dumps(rendered)) == [
        {
            "env": _ENV,
            "args": _ARGS,
            "command": "echo test",
            "timeout": 3,
            "_apm_source": "pkg",
        }
    ]


@pytest.mark.parametrize(
    "render",
    [*_NESTED_RENDERERS, lambda entries: _to_antigravity_hook_entries(entries, "SessionStart")],
    ids=["claude", "codex", "gemini", "antigravity-nested", "antigravity-flat"],
)
def test_raw_entries_are_materialized_independently(render: Callable[[list], list]) -> None:
    """Each raw-entry adapter returns mutable JSON detached from source arrays."""
    raw = [{"env": {"APM_TEST": "value"}}, ["x", {"y": [1]}]]
    source = copy.deepcopy(raw)

    rendered = render([raw])

    assert json.loads(json.dumps(rendered)) == [source]
    rendered[0][0]["env"]["APM_TEST"] = "mutated"
    rendered[0][1][1]["y"].append(2)
    assert raw == source
    assert render([raw]) == [source]


def test_rendered_binding_handler_and_raw_output_are_independent_of_ir_and_source() -> None:
    """Mutation of binding, handler and raw containers cannot reach source or IR."""
    entries = [*_nested_entries(), [{"env": {"A": ["1"]}}]]
    source = copy.deepcopy(entries)
    document = _entries_to_ir(entries)

    first = _render_nested_document(document, timeout_milliseconds=False)
    first[0]["labels"]["team"].append("mutated")
    first[0]["hooks"][0]["env"]["NESTED"]["LEVEL"][1]["b"] = 2
    first[0]["hooks"][0]["args"].clear()
    first[1][0]["env"]["A"].append("mutated")

    assert entries == source
    assert (
        json.loads(json.dumps(_render_nested_document(document, timeout_milliseconds=False)))
        == source
    )
