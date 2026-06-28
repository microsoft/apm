"""Round-31 PARSER/CHAOS -- plugin-package JSON parsers reachable from install.

DOMAIN: a hostile dependency package (a cloned git repo / local plugin dir)
that APM normalizes during ``apm install`` ships a malicious ``plugin.json``
or ``.mcp.json`` / ``.lsp.json``. These files are parsed by the REAL
``apm_cli.deps.plugin_parser`` functions that ``apm install`` calls via
``apm_cli/install/sources.py`` -> ``normalize_plugin_directory``.

Unlike the apm.yml family, these JSON parsers do NOT route through the
size/complexity-bounded loader and they catch only ``json.JSONDecodeError``
(and ``OSError``). Two stdlib-json failure classes therefore escape:

  * deep nesting ``[[[[...]]]]`` -> ``RecursionError``
  * a >4300-digit integer literal -> ``ValueError`` (CPython
    ``int_max_str_digits`` parse-time guard), which is NOT a
    ``json.JSONDecodeError`` subclass.

Either one propagates as an uncaught traceback out of the install fire path
-- a fail-OPEN crash of a default command. Same break class as the
round-30 ``config_loader`` raw-``safe_load`` finding.

Each test asserts the SECURE behavior (graceful, fail-closed, fast). On HEAD
they ERROR/FAIL (red-before); once the parsers catch ``RecursionError`` /
``ValueError`` (or route through a bounded JSON reader) they pass. A benign
control proves normal plugin manifests still parse.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .conftest import run_guarded

# A deeply-nested JSON document: triggers RecursionError in the stdlib C
# scanner. Kept off the apm.yml bounded path entirely.
_DEEP = "[" * 60000 + "]" * 60000

# A >4300-digit integer literal: json materializes it as a Python int and
# CPython raises a bare ValueError (not JSONDecodeError) at parse time.
_HUGE_INT = "1" + "0" * 4400


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Vector A: malicious .mcp.json (auto-discovered) crashes synthesize/install.
#   plugin_parser._read_mcp_json catches only (JSONDecodeError, OSError).
# ---------------------------------------------------------------------------


def test_mcp_json_deep_nest_does_not_crash_install(tmp_path):
    """A deep-nested .mcp.json must fail closed, not raise RecursionError."""
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    plugin = tmp_path / "evil_plugin"
    plugin.mkdir()
    _write(plugin / ".mcp.json", _DEEP)

    start = time.monotonic()
    # plugin_json_path=None -> synthesize path runs and auto-discovers .mcp.json
    finished, _result, exc = run_guarded(
        lambda: normalize_plugin_directory(plugin, None), timeout=3.0
    )
    elapsed = time.monotonic() - start

    assert finished, "normalize_plugin_directory hung on deep-nested .mcp.json"
    assert elapsed < 3.0, f"parse took too long: {elapsed:.2f}s"
    # SECURE expectation: no exception escapes; a malformed pass-through file
    # is skipped with a warning, not a crash. RED on HEAD (RecursionError).
    assert exc is None, f"install crashed on hostile .mcp.json: {exc!r}"


def test_mcp_json_huge_int_does_not_crash_install(tmp_path):
    """A >4300-digit int in .mcp.json must fail closed, not raise ValueError."""
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    plugin = tmp_path / "evil_plugin_int"
    plugin.mkdir()
    _write(plugin / ".mcp.json", '{"mcpServers": {"x": {"command": ' + _HUGE_INT + "}}}")

    start = time.monotonic()
    finished, _result, exc = run_guarded(
        lambda: normalize_plugin_directory(plugin, None), timeout=3.0
    )
    elapsed = time.monotonic() - start

    assert finished, "normalize_plugin_directory hung on huge-int .mcp.json"
    assert elapsed < 3.0, f"parse took too long: {elapsed:.2f}s"
    assert exc is None, f"install crashed on huge-int .mcp.json: {exc!r}"


# ---------------------------------------------------------------------------
# Vector B: malicious .lsp.json (auto-discovered) -- same uncaught classes.
#   plugin_parser._read_lsp_json catches only (JSONDecodeError, OSError).
# ---------------------------------------------------------------------------


def test_lsp_json_deep_nest_does_not_crash_install(tmp_path):
    """A deep-nested .lsp.json must fail closed, not raise RecursionError."""
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    plugin = tmp_path / "evil_lsp"
    plugin.mkdir()
    _write(plugin / ".lsp.json", _DEEP)

    finished, _result, exc = run_guarded(
        lambda: normalize_plugin_directory(plugin, None), timeout=3.0
    )

    assert finished, "normalize hung on deep-nested .lsp.json"
    assert exc is None, f"install crashed on hostile .lsp.json: {exc!r}"


# ---------------------------------------------------------------------------
# Vector C: malicious plugin.json (deep nest) escapes normalize's catch.
#   parse_plugin_manifest catches only JSONDecodeError; normalize_plugin_
#   directory wraps it in `except (ValueError, FileNotFoundError)` -- so a
#   RecursionError is NOT caught and propagates.
# ---------------------------------------------------------------------------


def test_plugin_json_deep_nest_does_not_crash_install(tmp_path):
    """A deep-nested plugin.json must fail closed, not raise RecursionError."""
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    plugin = tmp_path / "evil_manifest"
    plugin.mkdir()
    pj = _write(plugin / "plugin.json", _DEEP)

    finished, _result, exc = run_guarded(
        lambda: normalize_plugin_directory(plugin, pj), timeout=3.0
    )

    assert finished, "normalize hung on deep-nested plugin.json"
    # normalize catches (ValueError, FileNotFoundError) but NOT RecursionError.
    assert exc is None, f"install crashed on hostile plugin.json: {exc!r}"


# ---------------------------------------------------------------------------
# Benign control: a normal plugin manifest + .mcp.json still parse correctly.
# ---------------------------------------------------------------------------


def test_benign_plugin_still_synthesizes(tmp_path):
    """A well-formed plugin must still synthesize apm.yml without error."""
    from apm_cli.deps.plugin_parser import normalize_plugin_directory

    plugin = tmp_path / "good_plugin"
    plugin.mkdir()
    pj = _write(plugin / "plugin.json", json.dumps({"name": "good", "version": "1.0.0"}))
    _write(
        plugin / ".mcp.json",
        json.dumps({"mcpServers": {"echo": {"command": "echo", "args": ["hi"]}}}),
    )

    finished, result, exc = run_guarded(lambda: normalize_plugin_directory(plugin, pj), timeout=3.0)

    assert finished and exc is None, f"benign plugin failed: {exc!r}"
    assert isinstance(result, Path) and result.name == "apm.yml"
    assert result.exists()
