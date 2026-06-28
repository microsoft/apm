"""r3-parser-2: a deeply nested policy JSON crashes the parser/validator.

``json.load`` / ``json.loads`` recurse one C frame per nesting level and
raise ``RecursionError`` on a deep document. The discovery read site
(``parse_script_file``) and the ``apm lifecycle validate`` JSON branch
(``_validate_script_file``) caught only ``json.JSONDecodeError`` (and OS /
decode errors), so a ``RecursionError`` escaped -- crashing discovery (and
the install-time firing path) and ``validate`` with an unhandled traceback.

A pathologically nested JSON file is malformed input: discovery must degrade
to an empty list and validate must return a structured error, never an
unhandled traceback. These tests assert the SAFE contract.
"""

from __future__ import annotations

from .conftest import run_guarded, write_bytes

# Nesting far past CPython's default recursion limit (~1000).
_DEEP = 20_000


def _deep_json_array(depth: int = _DEEP) -> bytes:
    return ("[" * depth + "]" * depth).encode("utf-8")


def _deep_json_object(depth: int = _DEEP) -> bytes:
    return ('{"a":' * depth + "1" + "}" * depth).encode("utf-8")


def test_discovery_survives_deep_json(policy_dir) -> None:
    from apm_cli.core.lifecycle_scripts import parse_script_file

    target = write_bytes(policy_dir / "10-deep.json", _deep_json_array())

    finished, result, exc = run_guarded(lambda: parse_script_file(target, "policy"))

    assert finished, "parse_script_file hung on deep JSON"
    assert exc is None, f"deep JSON escaped as unhandled {type(exc).__name__}"
    assert result == []


def test_validate_survives_deep_json(tmp_path) -> None:
    from apm_cli.commands.lifecycle import _validate_script_file

    target = write_bytes(tmp_path / "deep.json", _deep_json_object())

    finished, result, exc = run_guarded(lambda: _validate_script_file(target, "policy"))

    assert finished, "_validate_script_file hung on deep JSON"
    assert exc is None, f"deep JSON escaped as unhandled {type(exc).__name__}"
    assert isinstance(result, list)
    assert result, "a malformed deep-nested JSON must yield a structured error"
    assert any("Invalid JSON" in msg for msg in result)
