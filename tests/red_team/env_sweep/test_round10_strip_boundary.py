"""Round-10 env break r10-env-1: whitespace-boundary redaction bypass.

A credential value whose env value carries boundary whitespace (trailing
``\\n`` / space / ``\\r`` / ``\\t`` or a leading ``\\n``) used to leak the
cleartext core to ``scripts.log``: ``_append_to_script_log`` redacted
``stdout.strip()`` -- ``str.strip()`` mutated the haystack BEFORE
``_redact_secrets`` ran, so the exact-value ``str.replace`` needle (e.g.
``value\\n``) no longer appeared in the stripped buffer and the core value
was written verbatim into APM's own 0600 audit log.

The fix redacts the RAW buffer and strips for display AFTER:
``_redact_embedded_url_credentials(_redact_secrets(stdout)).strip()``.

These traps fire the real on-disk log writer and assert the 30-char core
secret never reaches ``$APM_HOME/logs/scripts.log`` for any boundary
variant, while the no-whitespace control stays masked too (proving the
variable IS name-recognized and this is a mechanism flaw, not a name gap).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se

# 30-char core: well above _MIN_REDACT_LEN, so masking is in scope.
_CORE = "CORE" + "X" * 26


def _read_log() -> str:
    return se._get_scripts_log_path().read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("trailing-newline", _CORE + "\n"),
        ("leading-newline", "\n" + _CORE),
        ("trailing-space", _CORE + " "),
        ("carriage-return", _CORE + "\r"),
        ("tab", _CORE + "\t"),
        ("crlf", _CORE + "\r\n"),
    ],
)
def test_whitespace_boundary_value_is_masked(monkeypatch, tmp_path, label, value):
    """A credential value bracketed by boundary whitespace must not leak."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("MY_TOKEN", value)

    se._append_to_script_log(
        "post-install",
        "command",
        "echo",
        stdout=value,
        status="ok",
        exit_code=0,
    )

    content = _read_log()
    assert _CORE not in content, f"{label}: cleartext core leaked to scripts.log"
    assert "[REDACTED]" in content, f"{label}: redaction marker absent"


def test_no_whitespace_control_is_masked(monkeypatch, tmp_path):
    """The same name with NO boundary whitespace was always masked.

    Confirms the variable is credential-name-recognized, so the boundary
    leak was a strip()-ordering mechanism flaw, not a denylist-name gap.
    """
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("MY_TOKEN", _CORE)

    se._append_to_script_log(
        "post-install",
        "command",
        "echo",
        stdout=_CORE,
        status="ok",
        exit_code=0,
    )

    assert _CORE not in _read_log()


def test_name_is_recognized_as_credential():
    """Direct assertion that MY_TOKEN matches the credential matcher."""
    assert se._matches_credential("MY_TOKEN")


def test_boundary_value_in_stderr_is_masked(monkeypatch, tmp_path):
    """The stderr path shares the same redact-then-strip fix."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("MY_TOKEN", _CORE + "\n")

    se._append_to_script_log(
        "post-install",
        "command",
        "echo",
        stderr=_CORE + "\n",
        status="error",
        exit_code=1,
    )

    assert _CORE not in _read_log()
