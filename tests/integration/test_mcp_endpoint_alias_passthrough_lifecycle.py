"""Harness endpoint aliases in MCP passthrough keep the modeled url (0.36.1 harness track).

Gemini and Antigravity read endpoint keys named ``httpUrl`` and ``serverUrl``.
Those names alias the modeled ``url`` field, so a manifest ``extra:`` block or
an unknown top-level key carrying them is dropped with the reserved-key
warning, and every rendered entry keeps exactly one endpoint: the declared one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import HarnessScene, describe, warning_segments

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_DECLARED = "https://declared.example.invalid/mcp"
_ALIAS_VALUE = "https://alias.example.invalid/mcp"
_ENDPOINT_KEYS = {"url", "httpUrl", "serverUrl"}


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path)
    try:
        yield result
    finally:
        result.close()


@pytest.mark.parametrize("placement", ["extra-block", "top-level"])
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_endpoint_alias_keys_never_reach_native_config(
    scene: HarnessScene, transport: str, placement: str
) -> None:
    aliases = {"httpUrl": _ALIAS_VALUE, "serverUrl": _ALIAS_VALUE}
    declaration = {"name": "alias-fixture", "registry": False, "transport": transport}
    declaration["url"] = _DECLARED
    if placement == "extra-block":
        declaration["extra"] = aliases
    else:
        declaration.update(aliases)
    scene.write_manifest([declaration], user=False)
    harnesses = ("gemini", "antigravity", "copilot", "claude", "vscode")

    result = scene.install(",".join(harnesses), user=False)

    assert result.returncode == 0, describe(result)
    for harness in harnesses:
        entry = scene.servers(harness, user=False).get("alias-fixture")
        if entry is None:
            assert harness == "antigravity" and transport == "sse", harness
            continue
        endpoints = {key: entry[key] for key in _ENDPOINT_KEYS & entry.keys()}
        assert list(endpoints.values()) == [_DECLARED], f"{harness}: {entry}"
    warnings = warning_segments(result)
    assert any(
        "reserved passthrough key" in seg and "httpUrl" in seg and "serverUrl" in seg
        for seg in warnings
    ), warnings
