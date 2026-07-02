"""RED-TEAM: non-ASCII, control, and NUL bytes in apm.yml fields.

Two contracts:

1. Raw control bytes (NUL, ESC, BELL) in the manifest text make PyYAML's
   reader reject the stream; ``parse_apm_yml_lifecycle`` catches the load
   error and degrades to an empty list -- it must never raise or hang.
2. Legitimate UTF-8 content in a field value (an accented description, an
   emoji) parses cleanly into a Python str -- the parser is encoding-safe.

All non-ASCII bytes are written explicitly as escaped ``bytes`` so this
test SOURCE stays printable-ASCII per the repo encoding rule.
"""

from __future__ import annotations

import pytest

from .conftest import write_text_bytes

CMD_PREFIX = b'lifecycle:\n  post-install:\n    - type: command\n      bash: "echo '
CMD_SUFFIX = b'"\n'


def _parse(path):
    from apm_cli.core.lifecycle_scripts import parse_apm_yml_lifecycle

    return parse_apm_yml_lifecycle(path, "project")


@pytest.mark.parametrize(
    "ctrl",
    [b"\x00", b"\x1b", b"\x07", b"\x01", b"\x1f"],
    ids=["nul", "esc", "bell", "soh", "us"],
)
def test_control_bytes_degrade_to_empty(tmp_path, ctrl):
    doc = write_text_bytes(tmp_path / "apm.yml", CMD_PREFIX + ctrl + b"x" + CMD_SUFFIX)
    try:
        result = _parse(doc)
    except Exception as exc:
        pytest.fail(f"parser raised on control byte {ctrl!r}: {type(exc).__name__}: {exc}")
    assert result == []


def test_utf8_description_parses_cleanly(tmp_path):
    # b'\xc3\xa9\xf0\x9f\x9a\x80' decodes to an accented 'e' followed by a rocket.
    utf8_value = b"\xc3\xa9\xf0\x9f\x9a\x80"
    content = (
        b"lifecycle:\n"
        b"  post-install:\n"
        b"    - type: command\n"
        b"      bash: echo hi\n"
        b'      description: "' + utf8_value + b'"\n'
    )
    doc = write_text_bytes(tmp_path / "apm.yml", content)
    result = _parse(doc)
    assert len(result) == 1
    assert result[0].description == utf8_value.decode("utf-8")


def test_utf8_in_command_string_parses(tmp_path):
    utf8_value = b"\xc3\xb1"  # n-with-tilde
    doc = write_text_bytes(tmp_path / "apm.yml", CMD_PREFIX + utf8_value + CMD_SUFFIX)
    result = _parse(doc)
    assert len(result) == 1
    assert result[0].bash == "echo " + utf8_value.decode("utf-8")
