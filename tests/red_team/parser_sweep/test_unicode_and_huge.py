"""RED-TEAM round-2: unicode / control bytes + huge / deeply-nested input.

Vectors 5 and 6: control bytes (NUL, BIDI overrides, zero-width, CRLF,
BOM) in keys / values / event names must not crash the parser nor inject
into logs, and a huge / deeply-nested manifest must parse in bounded time
(no pathological blow-up) with validate still completing.
"""

from __future__ import annotations

from .conftest import run_guarded, write_apm_yml


def _parse(tmp_path, body):
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    path = write_apm_yml(tmp_path, body)
    return parse_apm_yml_lifecycle(path, "project")


def test_control_bytes_in_values_do_not_crash(tmp_path):
    """BIDI / zero-width / CRLF inside command values parse without crash."""
    body = (
        "lifecycle:\n"
        "  post-install:\n"
        '    - {type: command, command: "echo \u202eevil\u202c\u200b\r\nmore"}\n'
    )
    finished, result, exc = run_guarded(lambda: _parse(tmp_path, body), timeout=6.0)
    assert finished and exc is None, f"control bytes crashed the parser: {exc!r}"
    assert isinstance(result, list)


def test_control_bytes_in_event_name_are_ignored(tmp_path):
    """A control-laced event name is unknown -> entry skipped, no crash."""
    body = 'lifecycle:\n  "post-install\u202e":\n    - {type: command, command: echo hi}\n'
    finished, result, exc = run_guarded(lambda: _parse(tmp_path, body), timeout=6.0)
    assert finished and exc is None
    # The mangled event name is not in LIFECYCLE_EVENTS, so nothing is built.
    assert result == []


def test_bom_prefixed_utf8_yaml_parses(tmp_path):
    """A UTF-8 BOM-prefixed apm.yml (still valid UTF-8) must parse, not crash."""
    path = tmp_path / "apm.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "lifecycle:\n  post-install:\n    - {type: command, command: echo hi}\n"
    path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))

    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    finished, result, exc = run_guarded(
        lambda: parse_apm_yml_lifecycle(path, "project"), timeout=6.0
    )
    assert finished and exc is None, f"BOM apm.yml crashed the parser: {exc!r}"
    assert isinstance(result, list)


def test_huge_flat_manifest_is_bounded(tmp_path):
    """A manifest with many entries parses in bounded wall-clock time."""
    entries = "\n".join(f"    - {{type: command, command: echo {i}}}" for i in range(5000))
    body = f"lifecycle:\n  post-install:\n{entries}\n"
    finished, result, exc = run_guarded(lambda: _parse(tmp_path, body), timeout=8.0)
    assert finished, "huge flat manifest exceeded the parse-time bound"
    assert exc is None, f"huge manifest crashed the parser: {exc!r}"
    assert len(result) == 5000


def test_deeply_nested_value_is_bounded(tmp_path):
    """A deeply-nested command value (nested lists) must not blow up parse/validate."""
    nested = "[" * 200 + "1" + "]" * 200
    body = f"lifecycle:\n  post-install:\n    - {{type: command, command: {nested}}}\n"
    finished, _result, exc = run_guarded(lambda: _parse(tmp_path, body), timeout=8.0)
    assert finished, "deeply-nested value exceeded the parse-time bound"
    assert exc is None, f"deeply-nested value crashed the parser: {exc!r}"

    from apm_cli.commands.lifecycle import _validate_script_file

    path = tmp_path / "apm.yml"
    finished2, errors, exc2 = run_guarded(
        lambda: _validate_script_file(path, "project"), timeout=8.0
    )
    assert finished2, "validate did not complete on a deeply-nested manifest"
    assert exc2 is None, f"validate crashed on a deeply-nested manifest: {exc2!r}"
    assert isinstance(errors, list)
