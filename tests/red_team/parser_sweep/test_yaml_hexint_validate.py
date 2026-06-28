"""Round-6 (r6-parser-1) trap: YAML hex/octal int literal must not crash validate.

YAML hex/octal/sexagesimal integer literals use power-of-2 (or compositional)
bases that BYPASS CPython's ``int_max_str_digits`` limit at parse time --
``int("0xff...", 16)`` is unbounded -- so ``yaml.safe_load`` materializes an
arbitrarily large ``int``. ``apm lifecycle validate`` then interpolated that
value into an error f-string, where ``str(int)`` forces a DECIMAL conversion
that DOES hit the 4300-digit limit and raised an uncaught ``ValueError``
(a traceback instead of the structured report).

Round 5 closed the JSON/decimal tier (r5-parser-1); this is the YAML tier's
``type`` value and event-key sites. The fix routes both through
``_safe_token``, which never forces a decimal ``str(int)`` on a hostile value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# >4300 decimal digits once converted: 5000 hex 'f' nibbles ~= 6020 decimal
# digits, comfortably past the limit; the literal itself parses fine in YAML.
_HEX_BOMB = "0x" + "f" * 5000
_OCT_BOMB = "0o" + "7" * 6000


def _validate(tmp_path: Path, body: str) -> list[str]:
    from apm_cli.commands.lifecycle import _validate_script_file

    path = tmp_path / "apm.yml"
    path.write_text(body, encoding="utf-8")
    return _validate_script_file(path, "project")


@pytest.mark.parametrize("bomb", [_HEX_BOMB, _OCT_BOMB])
def test_yaml_oversized_int_type_does_not_crash_validate(tmp_path, bomb):
    """A non-string oversized-int 'type' must be a structured error, not a crash."""
    body = f"lifecycle:\n  post-install:\n    - type: {bomb}\n"
    try:
        errors = _validate(tmp_path, body)
    except Exception as exc:
        pytest.fail(f"BREAK: validate raised on oversized-int type: {type(exc).__name__}: {exc}")
    assert isinstance(errors, list)
    assert any("unknown type" in e for e in errors), errors


@pytest.mark.parametrize("bomb", [_HEX_BOMB, _OCT_BOMB])
def test_yaml_oversized_int_event_key_does_not_crash_validate(tmp_path, bomb):
    """A non-string oversized-int event KEY must be a structured error, not a crash."""
    body = f"lifecycle:\n  ? {bomb}\n  : []\n"
    try:
        errors = _validate(tmp_path, body)
    except Exception as exc:
        pytest.fail(f"BREAK: validate raised on oversized-int key: {type(exc).__name__}: {exc}")
    assert isinstance(errors, list)
    assert any("Unknown event" in e for e in errors), errors


def test_cli_validate_oversized_int_type_exits_clean(tmp_path, monkeypatch):
    """End-to-end: the CLI command exits via SystemExit, not a ValueError traceback."""
    import os

    from click.testing import CliRunner

    from apm_cli.commands.lifecycle import lifecycle

    monkeypatch.setenv("APM_E2E_TESTS", "1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        f"lifecycle:\n  post-install:\n    - type: {_HEX_BOMB}\n", encoding="utf-8"
    )
    result = CliRunner().invoke(lifecycle, ["validate"])
    assert not isinstance(result.exception, ValueError), result.output
    # exit code 1 (validation errors) via SystemExit is the clean structured path.
    assert result.exit_code == 1, (result.exit_code, result.output)
    assert os.path.exists(home)  # sanity: APM_HOME honored, no stray write
