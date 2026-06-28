"""Round-22 red-team parser probe: untrusted JSON readers on the ``apm pack``
plugin-export surface must fail closed, not crash.

Prior rounds hardened the bundle JSON readers
(``read_bundle_plugin_json`` / ``_parse_bundle_mcp_servers``) to the wide
``except (OSError, ValueError, RecursionError)`` posture so a hostile
``plugin.json`` / ``.mcp.json`` in a freshly-cloned UNTRUSTED repo fails
closed instead of crashing ``apm install`` / ``apm pack``.

But three sibling readers on the SAME default-on ``apm pack`` export path
still catch only the NARROW ``except (json.JSONDecodeError, OSError)``:

  * ``apm_cli.core.plugin_manifest.collect_mcp_servers`` (reads ``.mcp.json``)
  * ``apm_cli.core.plugin_manifest.find_or_synthesize_plugin_json``
    (reads ``plugin.json``)
  * ``apm_cli.bundle.plugin_exporter._collect_hooks_from_root``
    (reads ``hooks.json`` / ``hooks/*.json``)

``json.JSONDecodeError`` is a ``ValueError`` subclass, but two hostile JSON
shapes raise something OTHER than a ``JSONDecodeError``:

  * an oversized integer literal (``{"x": <5000 digits>}``) raises a BARE
    ``ValueError`` ("Exceeds the limit (4300 digits) for integer string
    conversion") past CPython's ``int_max_str_digits`` -- NOT a
    ``JSONDecodeError``;
  * a deeply nested array (``[[[ ... ]]]``) raises ``RecursionError``.

Both escape ``except (json.JSONDecodeError, OSError)`` and crash the
``apm pack`` export with an uncaught traceback -- a hostile committed file
turns a pack into a whole-run abort instead of the intended fail-closed
skip.

Each reader is designed to RETURN A SAFE EMPTY/SYNTHESISED VALUE on a bad
file; the secure contract asserted here is exactly that, fast (<~1s), with
no raise and no hang. A benign manifest still parses normally.
"""

from __future__ import annotations

import json
import threading

import pytest

# Longer than the default int_max_str_digits (4300) limit -> bare ValueError.
_OVERSIZED_INT = "9" * 5000


def _run_bounded(fn, timeout: float = 5.0):
    """Run ``fn`` on a daemon thread; fail if it hangs past *timeout*.

    Returns ``("ok", value)`` on a clean return, ``("raised", exc)`` if the
    callable raised, or fails the test outright on a hang (the runtime bans
    the ``timeout`` shell command, so DoS detection is in-process).
    """
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
            box["status"] = "ok"
        except BaseException as exc:
            box["exc"] = exc
            box["status"] = "raised"

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"BREAK: reader hung (>{timeout:.0f}s) on hostile JSON -- parse-time DoS")
    if box.get("status") == "raised":
        return "raised", box["exc"]
    return "ok", box.get("value")


# --------------------------------------------------------------------------
# collect_mcp_servers (.mcp.json) -- reached by apm pack via build_plugin_manifest
# --------------------------------------------------------------------------


def test_collect_mcp_servers_oversized_int_fails_closed(tmp_path):
    from apm_cli.core.plugin_manifest import collect_mcp_servers

    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"a": {"x": ' + _OVERSIZED_INT + "}}}",
        encoding="utf-8",
    )
    status, result = _run_bounded(lambda: collect_mcp_servers(tmp_path))
    if status == "raised":
        pytest.fail(
            "BREAK: collect_mcp_servers raised on oversized-int .mcp.json: "
            f"{type(result).__name__}: {result}"
        )
    assert result == {}, f"expected fail-closed empty dict, got {result!r}"


def test_collect_mcp_servers_deep_nesting_fails_closed(tmp_path):
    from apm_cli.core.plugin_manifest import collect_mcp_servers

    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": ' + "[" * 50000 + "}",
        encoding="utf-8",
    )
    status, result = _run_bounded(lambda: collect_mcp_servers(tmp_path))
    if status == "raised":
        pytest.fail(
            f"BREAK: collect_mcp_servers raised on deeply-nested .mcp.json: {type(result).__name__}"
        )
    assert result == {}


# --------------------------------------------------------------------------
# _collect_hooks_from_root (hooks.json) -- reached directly by export_plugin_bundle
# --------------------------------------------------------------------------


def test_collect_hooks_oversized_int_fails_closed(tmp_path):
    from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

    (tmp_path / "hooks.json").write_text(
        '{"x": ' + _OVERSIZED_INT + "}",
        encoding="utf-8",
    )
    status, result = _run_bounded(lambda: _collect_hooks_from_root(tmp_path))
    if status == "raised":
        pytest.fail(
            "BREAK: _collect_hooks_from_root raised on oversized-int hooks.json: "
            f"{type(result).__name__}: {result}"
        )
    assert result == {}


def test_collect_hooks_deep_nesting_fails_closed(tmp_path):
    from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

    (tmp_path / "hooks.json").write_text("[" * 50000, encoding="utf-8")
    status, result = _run_bounded(lambda: _collect_hooks_from_root(tmp_path))
    if status == "raised":
        pytest.fail(
            "BREAK: _collect_hooks_from_root raised on deeply-nested hooks.json: "
            f"{type(result).__name__}"
        )
    assert result == {}


# --------------------------------------------------------------------------
# find_or_synthesize_plugin_json (plugin.json) -- apm pack disk-first reader
# --------------------------------------------------------------------------


def test_find_or_synthesize_oversized_int_falls_back(tmp_path):
    from apm_cli.core.plugin_manifest import find_or_synthesize_plugin_json

    (tmp_path / "plugin.json").write_text(
        '{"x": ' + _OVERSIZED_INT + "}",
        encoding="utf-8",
    )
    (tmp_path / "apm.yml").write_text("name: probe\nversion: 1.0.0\n", encoding="utf-8")

    status, result = _run_bounded(
        lambda: find_or_synthesize_plugin_json(tmp_path, tmp_path / "apm.yml")
    )
    if status == "raised":
        pytest.fail(
            "BREAK: find_or_synthesize_plugin_json raised on oversized-int plugin.json: "
            f"{type(result).__name__}: {result}"
        )
    # Contract: a bad on-disk plugin.json warns and falls back to synthesis
    # from apm.yml, so a dict is still returned.
    assert isinstance(result, dict)


# --------------------------------------------------------------------------
# Benign manifests still parse normally (no false-positive regression).
# --------------------------------------------------------------------------


def test_benign_mcp_and_hooks_still_parse(tmp_path):
    from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root
    from apm_cli.core.plugin_manifest import collect_mcp_servers

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": "node", "args": ["x.js"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "hooks.json").write_text(
        json.dumps({"PreToolUse": [{"matcher": "Bash"}]}),
        encoding="utf-8",
    )
    mcp = collect_mcp_servers(tmp_path)
    hooks = _collect_hooks_from_root(tmp_path)
    assert "svc" in mcp
    assert "PreToolUse" in hooks
