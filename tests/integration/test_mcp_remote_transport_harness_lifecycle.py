"""Remote MCP transports render in each harness's native shape (0.36.1 harness track).

Covers #3736 (registry v0.1 ``remotes[].type`` ignored), #3711 part 2
(Antigravity needs ``serverUrl`` and cannot use SSE) and the per-harness SSE
rows of the approved shape table (Copilot, Claude, VS Code, Gemini, Codex,
Hermes per #3749). Harnesses outside the changed rows (Cursor, Kiro, OpenCode, Windsurf)
are regression locks: their v0.36.0 bytes must not move.

Every scenario drives the installed ``apm`` binary against a temp HOME and a
local MCP Registry v0.1 endpoint; nothing reaches a real host.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import (
    HarnessScene,
    describe,
    flat,
    registry_document,
    warning_segments,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_REGISTRY_NAME = "io.github.apm-fixture/remote-fixture"
_SHORT_NAME = "remote-fixture"
_URL = "https://mcp.example.invalid/endpoint"
_CHANGED = ("copilot", "claude", "gemini", "antigravity", "codex", "hermes")
_UNSUPPORTED_SSE = {"antigravity", "codex"}


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path)
    try:
        yield result
    finally:
        result.close()


def _assert_remote_entry(
    scene: HarnessScene,
    harness: str,
    *,
    key: str,
    transport: str,
    url: str,
    user: bool,
) -> None:
    """Assert one harness entry against the approved harness x transport table."""
    servers = scene.servers(harness, user=user)
    if transport == "sse" and harness in _UNSUPPORTED_SSE:
        assert key not in servers, f"{harness} must not receive an SSE remote: {servers}"
        return
    assert key in servers, f"{harness} missing {key!r}: {servers}"
    entry = servers[key]
    if harness in ("copilot", "claude"):
        expected_type = "sse" if transport == "sse" else "http"
        assert entry.get("type") == expected_type, f"{harness}: {entry}"
        assert entry.get("url") == url, f"{harness}: {entry}"
    elif harness == "gemini":
        if transport == "sse":
            assert entry.get("url") == url and "httpUrl" not in entry, entry
        else:
            assert entry.get("httpUrl") == url and "url" not in entry, entry
    elif harness == "antigravity":
        assert entry.get("serverUrl") == url, f"antigravity: {entry}"
        assert "httpUrl" not in entry, f"antigravity rejects httpUrl: {entry}"
    elif harness == "codex":
        assert entry.get("url") == url, f"codex: {entry}"
    elif harness == "hermes":
        assert entry.get("url") == url, f"hermes: {entry}"
        if transport == "sse":
            assert entry.get("transport") == "sse", f"hermes: {entry}"
        else:
            assert "transport" not in entry, f"hermes: {entry}"
    else:  # pragma: no cover - table guard
        raise AssertionError(f"no expectation for {harness}")


def _assert_unsupported_sse_not_claimed(
    scene: HarnessScene, result, *, server: str, short: str, harnesses: tuple[str, ...], user: bool
) -> None:
    claimed = scene.claimed_runtimes(server, user=user)
    warnings = warning_segments(result)
    for harness in _UNSUPPORTED_SSE & set(harnesses):
        assert harness not in claimed, f"{harness} claimed an SSE server it never wrote: {claimed}"
        assert any(short in seg and harness in seg.lower() for seg in warnings), (
            f"no skip warning naming {short} for {harness}: {warnings}"
        )


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_registry_remote_type_reaches_every_harness_in_its_native_shape(
    scene: HarnessScene, transport: str, user: bool
) -> None:
    """A registry v0.1 ``type`` remote is rendered per harness and stays idempotent."""
    registry = scene.start_registry(registry_document(_REGISTRY_NAME, _URL, transport=transport))
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=user)
    harnesses = _CHANGED

    result = scene.install(",".join(harnesses), user=user)

    assert result.returncode == 0, describe(result)
    for harness in harnesses:
        _assert_remote_entry(
            scene, harness, key=_SHORT_NAME, transport=transport, url=_URL, user=user
        )
    if transport == "sse":
        _assert_unsupported_sse_not_claimed(
            scene,
            result,
            server=_REGISTRY_NAME,
            short=_SHORT_NAME,
            harnesses=harnesses,
            user=user,
        )

    before = scene.native_bytes(harnesses, user=user)
    scene.ok(*result.command[1:])
    assert scene.native_bytes(harnesses, user=user) == before


def test_legacy_transport_type_sse_still_renders_as_sse(scene: HarnessScene) -> None:
    """The pre-v0.1 ``transport_type`` key keeps working after normalization."""
    registry = scene.start_registry(
        registry_document(_REGISTRY_NAME, _URL, transport_key="transport_type", transport="sse")
    )
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=False)
    harnesses = ("copilot", "claude", "gemini", "codex", "hermes")

    result = scene.install(",".join(harnesses), user=False)

    assert result.returncode == 0, describe(result)
    for harness in harnesses:
        _assert_remote_entry(scene, harness, key=_SHORT_NAME, transport="sse", url=_URL, user=False)


def test_registry_remote_without_transport_defaults_to_streamable_http(
    scene: HarnessScene,
) -> None:
    """A remote with no transport field keeps today's streamable-http default."""
    registry = scene.start_registry(registry_document(_REGISTRY_NAME, _URL, transport=None))
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=False)
    harnesses = ("copilot", "claude", "gemini", "codex", "hermes")

    result = scene.install(",".join(harnesses), user=False)

    assert result.returncode == 0, describe(result)
    for harness in harnesses:
        _assert_remote_entry(
            scene, harness, key=_SHORT_NAME, transport="streamable-http", url=_URL, user=False
        )


_SELF_DEFINED = (
    ("sd-sse", "sse", "https://sse.example.invalid/sse"),
    ("sd-http", "http", "https://http.example.invalid/mcp"),
    ("sd-streamable", "streamable-http", "https://streamable.example.invalid/mcp"),
)


@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_self_defined_remotes_render_natively_and_unsupported_sse_is_skipped(
    scene: HarnessScene, user: bool
) -> None:
    """Self-defined SSE/HTTP remotes: one unsupported harness never aborts the install."""
    declarations = [
        {"name": name, "registry": False, "transport": transport, "url": url}
        for name, transport, url in _SELF_DEFINED
    ]
    declarations.append(
        {"name": "sd-stdio", "registry": False, "transport": "stdio", "command": "node"}
    )
    scene.write_manifest(declarations, user=user)
    harnesses = _CHANGED

    result = scene.install(",".join(harnesses), user=user)

    assert result.returncode == 0, describe(result)
    for name, transport, url in _SELF_DEFINED:
        canonical = "sse" if transport == "sse" else "streamable-http"
        for harness in harnesses:
            _assert_remote_entry(scene, harness, key=name, transport=canonical, url=url, user=user)
    for harness in harnesses:
        assert "sd-stdio" in scene.servers(harness, user=user), f"{harness} lost later servers"
    _assert_unsupported_sse_not_claimed(
        scene, result, server="sd-sse", short="sd-sse", harnesses=harnesses, user=user
    )


# v0.36.0 renders, identical for both transports; the shape table keeps them.
_LOCKED_SHAPES = {
    "cursor": {"type": "http", "url": _URL},
    "kiro": {"url": _URL},
    "opencode": {"type": "remote", "enabled": True, "url": _URL},
    "windsurf": {"type": "http", "url": _URL, "tools": ["*"], "id": ""},
}


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_harnesses_outside_the_changed_rows_keep_their_remote_shape(
    scene: HarnessScene, transport: str, user: bool
) -> None:
    """Cursor, Kiro, OpenCode and Windsurf must not inherit a changed parent row."""
    registry = scene.start_registry(registry_document(_REGISTRY_NAME, _URL, transport=transport))
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=user)

    result = scene.install(",".join(_LOCKED_SHAPES), user=user)

    assert result.returncode == 0, describe(result)
    for harness, expected in _LOCKED_SHAPES.items():
        assert scene.servers(harness, user=user).get(_SHORT_NAME) == expected, harness
    # A registry install for Windsurf writes its user-wide file even at project
    # scope; the output names it there, as the self-defined route does (#3994).
    named = "user-wide: ~/.codeium/windsurf/mcp_config.json" in " ".join(result.stdout.split())
    assert named is not user, describe(result)


@pytest.mark.parametrize(
    ("transport", "label"), [("sse", "SSE"), ("streamable-http", "STREAMABLE-HTTP")]
)
def test_mcp_show_reports_the_registry_remote_transport(
    scene: HarnessScene, transport: str, label: str
) -> None:
    """``apm mcp show`` names the real transport instead of UNKNOWN."""
    registry = scene.start_registry(registry_document(_REGISTRY_NAME, _URL, transport=transport))
    env = {**scene.env, "MCP_REGISTRY_URL": registry}

    result = scene.ok("mcp", "show", _REGISTRY_NAME, env=env)

    text = flat(result.stdout)
    assert "UNKNOWN" not in text, text
    assert label in text.upper(), text


def test_frozen_replay_skips_a_registry_sse_server_its_harness_refuses(
    scene: HarnessScene,
) -> None:
    """An install that skipped an SSE server on Codex/Antigravity replays green under --frozen."""
    registry = scene.start_registry(registry_document(_REGISTRY_NAME, _URL, transport="sse"))
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=False)
    targets = "copilot,codex,antigravity"
    scene.ok("install", "--no-policy", "--target", targets)
    lock = (scene.project / "apm.lock.yaml").read_bytes()

    result = scene.run("install", "--no-policy", "--frozen", "--target", targets)

    assert result.returncode == 0, describe(result)
    assert (scene.project / "apm.lock.yaml").read_bytes() == lock
    warnings = warning_segments(result)
    for harness in _UNSUPPORTED_SSE:
        assert _SHORT_NAME not in scene.servers(harness, user=False), harness
        assert any(_REGISTRY_NAME in seg and harness in seg.lower() for seg in warnings), warnings


def test_frozen_deferred_pair_that_turns_out_supported_is_refused(scene: HarnessScene) -> None:
    """--frozen defers an unrecorded Codex pair (transport unknown offline), then refuses it.

    The registry remote declares no transport in apm.yml, so the structural
    pre-check defers (server, codex); once the registry payload shows Codex
    supports it, the MCP stage raises the same frozen error and writes nothing.
    """
    registry = scene.start_registry(
        registry_document(_REGISTRY_NAME, _URL, transport="streamable-http")
    )
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=False)
    scene.ok("install", "--no-policy", "--target", "copilot")

    result = scene.run("install", "--no-policy", "--frozen", "--target", "copilot,codex")

    assert result.returncode != 0, describe(result)
    assert "ownership is not recorded" in flat(result.stdout + result.stderr), describe(result)
    assert _SHORT_NAME not in scene.servers("codex", user=False)


def test_refused_frozen_replay_changes_no_native_config(scene: HarnessScene) -> None:
    """A deferred pair refused after the registry lookup leaves every native file untouched."""
    registry = scene.start_registry(
        registry_document(_REGISTRY_NAME, _URL, transport="streamable-http")
    )
    scene.write_manifest([{"name": _REGISTRY_NAME, "registry": registry}], user=False)
    scene.ok("install", "--no-policy", "--target", "copilot")
    copilot = scene.config_path("copilot", user=False)
    copilot.write_text('{"mcpServers": {}}', encoding="utf-8")
    before = scene.native_bytes(("copilot", "codex"), user=False)

    result = scene.run("install", "--no-policy", "--frozen", "--target", "copilot,codex")

    assert result.returncode != 0, describe(result)
    assert "ownership is not recorded" in flat(result.stdout + result.stderr), describe(result)
    assert scene.native_bytes(("copilot", "codex"), user=False) == before
