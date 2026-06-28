"""Round-5 (r5-parser-1) regression trap: oversized-int JSON must not crash.

A policy-tier JSON drop-in (``/etc/apm/policy.d/*.json``) containing an
integer literal longer than CPython's int-string-conversion limit (4300
digits) makes ``json.loads`` raise a *plain* ``ValueError`` -- NOT a
``JSONDecodeError`` -- at parse time. The round-4 narrow handlers
``except (JSONDecodeError, RecursionError)`` missed it, so the crash
escaped ``apm lifecycle validate``, script discovery, and the
install/update/uninstall fire path (build_runner_from_context).

This is the same uncaught-stdlib-ValueError bug class round-4 fixed for
``urlparse`` in validate; here it is closed for the JSON tier. Both parse
sites must now degrade gracefully (validate -> structured error, discovery
-> []), never raise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A literal longer than the default int_max_str_digits (4300) limit.
_OVERSIZED_INT = "1" + "0" * 100000


def _write_policy_json(tmp_path: Path, command_literal: str) -> Path:
    path = tmp_path / "00-policy.json"
    path.write_text(
        '{"version":1,"scripts":{"post-install":'
        '[{"type":"command","command":' + command_literal + "}]}}",
        encoding="utf-8",
    )
    return path


def test_validate_oversized_int_json_is_structured_not_crash(tmp_path):
    """validate must report the malformed JSON, never raise the bare ValueError."""
    from apm_cli.commands.lifecycle import _validate_script_file

    path = _write_policy_json(tmp_path, _OVERSIZED_INT)
    try:
        errors = _validate_script_file(path, "policy")
    except Exception as exc:
        pytest.fail(f"BREAK: validate raised on oversized-int JSON: {type(exc).__name__}: {exc}")
    assert isinstance(errors, list)
    assert any("Invalid JSON" in e for e in errors), errors


def test_parse_script_file_oversized_int_fails_closed(tmp_path):
    """Discovery's JSON parser must fail closed to [] so the fire path survives."""
    from apm_cli.core.lifecycle_scripts import parse_script_file

    path = _write_policy_json(tmp_path, _OVERSIZED_INT)
    try:
        entries = parse_script_file(path, "policy")
    except Exception as exc:
        pytest.fail(
            f"BREAK: parse_script_file raised on oversized-int JSON: {type(exc).__name__}: {exc}"
        )
    assert entries == []


def test_load_scripts_from_dir_skips_oversized_int_json(tmp_path):
    """Whole-directory discovery must not abort on one malformed policy file."""
    from apm_cli.core.lifecycle_scripts import _load_scripts_from_dir

    # A crashing oversized-int file alongside a well-formed sibling.
    _write_policy_json(tmp_path, _OVERSIZED_INT)
    good = tmp_path / "10-good.json"
    good.write_text(
        '{"version":1,"scripts":{"post-install":[{"type":"command","command":"echo ok"}]}}',
        encoding="utf-8",
    )
    try:
        entries = _load_scripts_from_dir(tmp_path, "policy")
    except Exception as exc:
        pytest.fail(
            f"BREAK: _load_scripts_from_dir raised on oversized-int JSON: "
            f"{type(exc).__name__}: {exc}"
        )
    commands = [e.command for e in entries if getattr(e, "command", None)]
    assert "echo ok" in commands, commands


@pytest.mark.parametrize(
    "bad_literal",
    [
        _OVERSIZED_INT,  # positive oversized int
        "-" + _OVERSIZED_INT,  # negative oversized int
    ],
)
def test_validate_oversized_int_variants_do_not_raise(tmp_path, bad_literal):
    from apm_cli.commands.lifecycle import _validate_script_file

    path = _write_policy_json(tmp_path, bad_literal)
    try:
        errors = _validate_script_file(path, "policy")
    except Exception as exc:
        pytest.fail(f"BREAK: validate raised on {bad_literal[:6]}...: {type(exc).__name__}: {exc}")
    assert isinstance(errors, list)
