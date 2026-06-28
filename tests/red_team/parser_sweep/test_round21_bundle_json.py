"""Round-21 red-team PARSER/CHAOS probe.

Target: the install-path bundle-metadata JSON readers
``read_bundle_plugin_json`` (bundle/local_bundle.py) and
``_parse_bundle_mcp_servers`` (install/local_bundle_handler.py), plus the
public ``detect_local_bundle`` entrypoint.

Reachability:
    apm install <bundle-dir-or-archive>
      -> commands.install (line ~1255) detect_local_bundle(probe)
      -> bundle.local_bundle._build_info
      -> read_bundle_plugin_json(bundle_dir)   # parses untrusted plugin.json
    apm install <bundle>
      -> install_local_bundle -> _wire_bundle_mcp_servers
      -> _parse_bundle_mcp_servers(bundle_dir) # parses untrusted .mcp.json

A local bundle (a downloaded .zip/.tar.gz/dir produced by some third party
and handed to ``apm install``) is UNTRUSTED -- the user did not hand-open
plugin.json / .mcp.json. The documented contract of these readers is
fail-closed: ``read_bundle_plugin_json`` returns ``{}`` "if missing" (and
on malformed JSON), ``_parse_bundle_mcp_servers`` returns ``[]`` on any
parse failure.

Both readers catch only ``(json.JSONDecodeError, OSError)``. But
``json.loads`` raises:
  * ``RecursionError`` on a deeply-nested document  (NOT a ValueError)
  * a *bare* ``ValueError`` on an integer literal past CPython's
    ``int_max_str_digits`` (4300) limit -- ``json.JSONDecodeError`` is a
    ValueError subclass but this is not a JSONDecodeError.

Neither is caught, so the readers RAISE instead of failing closed. Every
sibling loader in the codebase (``parse_script_file``, ``_load_trust_store``)
was uniformly widened to ``(OSError, ValueError, RecursionError)``; these two
bundle readers were missed.

SECURE contract asserted here (these are expected to FAIL on the current
hardened HEAD, demonstrating the gap):
  * read_bundle_plugin_json(hostile) -> {}      (never raises)
  * _parse_bundle_mcp_servers(hostile) -> []    (never raises)
  * detect_local_bundle(hostile_dir) does not raise RecursionError
    (the command's detection wrapper only catches ValueError).
"""

from __future__ import annotations

import threading
from pathlib import Path

from apm_cli.bundle.local_bundle import detect_local_bundle, read_bundle_plugin_json
from apm_cli.install.local_bundle_handler import _parse_bundle_mcp_servers


def _deep_json(depth: int = 30000) -> str:
    """A syntactically valid but pathologically deep JSON object string."""
    return '{"a":' * depth + "1" + "}" * depth


def _bigint_plugin_json(digits: int = 5000) -> str:
    """A valid plugin.json whose ``id`` value is an oversized integer."""
    return '{"id": ' + "9" * digits + "}"


def _run_guarded(fn, timeout: float = 30.0):
    """Run *fn* in a daemon thread; return (completed, result, exc)."""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["exc"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("result"), box.get("exc")


def _write_bundle(tmp_path: Path, *, plugin_json: str, mcp_json: str | None = None) -> Path:
    bundle = tmp_path / "evilbundle"
    bundle.mkdir()
    (bundle / "plugin.json").write_text(plugin_json, encoding="utf-8")
    if mcp_json is not None:
        (bundle / ".mcp.json").write_text(mcp_json, encoding="utf-8")
    return bundle


# ---------------------------------------------------------------------------
# read_bundle_plugin_json -- documented to return {} on missing/malformed
# ---------------------------------------------------------------------------


def test_read_bundle_plugin_json_deep_nesting_fails_closed(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, plugin_json=_deep_json())
    completed, result, exc = _run_guarded(lambda: read_bundle_plugin_json(bundle))
    assert completed, "read_bundle_plugin_json hung on deeply-nested plugin.json"
    assert exc is None, (
        f"SECURE CONTRACT BROKEN: read_bundle_plugin_json raised "
        f"{type(exc).__name__} on a hostile deeply-nested plugin.json instead "
        f"of failing closed to {{}}"
    )
    assert result == {}


def test_read_bundle_plugin_json_oversized_int_fails_closed(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, plugin_json=_bigint_plugin_json())
    completed, result, exc = _run_guarded(lambda: read_bundle_plugin_json(bundle))
    assert completed
    assert exc is None, (
        f"SECURE CONTRACT BROKEN: read_bundle_plugin_json raised "
        f"{type(exc).__name__} on an oversized-int plugin.json instead of "
        f"failing closed to {{}}"
    )
    assert result == {}


# ---------------------------------------------------------------------------
# _parse_bundle_mcp_servers -- documented best-effort, must return []
# ---------------------------------------------------------------------------


def test_parse_bundle_mcp_servers_deep_nesting_fails_closed(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        plugin_json='{"id": "ok"}',
        mcp_json=_deep_json(),
    )
    completed, result, exc = _run_guarded(lambda: _parse_bundle_mcp_servers(bundle))
    assert completed, "_parse_bundle_mcp_servers hung on deeply-nested .mcp.json"
    assert exc is None, (
        f"SECURE CONTRACT BROKEN: _parse_bundle_mcp_servers raised "
        f"{type(exc).__name__} on a hostile deeply-nested .mcp.json instead "
        f"of failing closed to []"
    )
    assert result == []


def test_parse_bundle_mcp_servers_oversized_int_fails_closed(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        plugin_json='{"id": "ok"}',
        mcp_json='{"mcpServers": {"x": {"port": ' + "9" * 5000 + "}}}",
    )
    completed, result, exc = _run_guarded(lambda: _parse_bundle_mcp_servers(bundle))
    assert completed
    assert exc is None, (
        f"SECURE CONTRACT BROKEN: _parse_bundle_mcp_servers raised "
        f"{type(exc).__name__} on an oversized-int .mcp.json instead of "
        f"failing closed to []"
    )
    assert result == []


# ---------------------------------------------------------------------------
# detect_local_bundle -- command-path entry; wrapper only catches ValueError
# ---------------------------------------------------------------------------


def test_detect_local_bundle_deep_nesting_not_recursionerror(tmp_path: Path) -> None:
    """The install command wraps detect_local_bundle in ``except ValueError``.

    A RecursionError from plugin.json escapes that wrapper (only the
    command's outermost broad ``except Exception`` contains it, mislabeled
    as 'Error installing dependencies: maximum recursion depth exceeded').
    Detection should fail closed (return None / a valid info), never raise
    RecursionError.
    """
    bundle = _write_bundle(tmp_path, plugin_json=_deep_json())
    completed, _result, exc = _run_guarded(lambda: detect_local_bundle(bundle))
    assert completed, "detect_local_bundle hung on deeply-nested plugin.json"
    assert not isinstance(exc, RecursionError), (
        "SECURE CONTRACT BROKEN: detect_local_bundle raised RecursionError on a "
        "hostile bundle plugin.json; the install command's detection wrapper "
        "only catches ValueError, so this escapes to the outer handler."
    )
