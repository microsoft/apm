"""Round-12 env break r12-env-1: CR-translation redaction bypass.

``_execute_command`` captures script output via ``subprocess.Popen(...,
text=True)``, whose universal-newline mode rewrites ``\\r\\n`` and lone
``\\r`` to ``\\n`` on read. ``_redact_secrets`` masks captured output by an
exact ``str.replace`` of each credential var's RAW ``os.environ`` value. A
credential whose value carries a carriage return (a CRLF-sourced ``.env``
var, a Windows PEM/base64 blob) therefore diverges from the captured buffer:
the raw needle still has ``\\r`` but the buffer does not, the replace misses,
and the cleartext core lands in the 0600 ``scripts.log``.

The fix masks the newline-normalized form of each secret too (the same
transform the subprocess applies), while keeping the raw form so the command
/ target string -- which is never newline-translated -- still matches.

These traps drive the REAL ``_append_to_script_log`` path and read the
on-disk ``scripts.log`` back, asserting the secret VALUE never appears in
cleartext, plus a unit-level ``_redact_secrets`` assertion. The no-CR
control through the identical path stays masked, proving a
redaction-mechanism flaw rather than a name/allowlist gap.
"""

from __future__ import annotations

import importlib

import pytest

se = importlib.import_module("apm_cli.core.script_executors")


def _read_log(apm_home):
    log = apm_home / "logs" / "scripts.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.fixture
def apm_home(tmp_path, monkeypatch):
    home = tmp_path / "apm_home"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


def test_crlf_credential_value_masked_in_log(apm_home, monkeypatch):
    """A CRLF-bearing credential must not leak cleartext to scripts.log."""
    monkeypatch.setenv("MY_DEPLOY_KEY", "SECRETLEFT123ABC\r\nSECRETRIGHT456XYZ")
    # What subprocess text=True actually hands _append_to_script_log:
    captured = "SECRETLEFT123ABC\nSECRETRIGHT456XYZ"
    se._append_to_script_log("post-install", "command", "deploy", stdout=captured)

    body = _read_log(apm_home)
    assert "SECRETLEFT123ABC" not in body, body
    assert "SECRETRIGHT456XYZ" not in body, body
    assert "[REDACTED]" in body


def test_lone_cr_credential_value_masked_in_log(apm_home, monkeypatch):
    """An old-Mac lone-CR credential must not leak either."""
    monkeypatch.setenv("MAC_TOKEN_KEY", "AAAA1111\rBBBB2222")
    captured = "AAAA1111\nBBBB2222"  # text=True translates lone CR -> LF
    se._append_to_script_log("post-install", "command", "deploy", stderr=captured)

    body = _read_log(apm_home)
    assert "AAAA1111" not in body, body
    assert "BBBB2222" not in body, body


def test_no_cr_control_still_masked(apm_home, monkeypatch):
    """The no-CR path must remain masked (mechanism, not name, regression)."""
    monkeypatch.setenv("PLAIN_API_KEY", "PLAINSECRET98765")
    se._append_to_script_log("post-install", "command", "deploy", stdout="echo PLAINSECRET98765")

    body = _read_log(apm_home)
    assert "PLAINSECRET98765" not in body, body
    assert "[REDACTED]" in body


def test_raw_command_string_still_masked(monkeypatch):
    """The raw (untranslated) command/target form must still match."""
    monkeypatch.setenv("RAW_REGISTRY_KEY", "RAWSECRET\r\nVALUE99")
    assert "RAWSECRET" not in se._redact_secrets("RAWSECRET\r\nVALUE99")


def test_non_credential_value_untouched(monkeypatch):
    """A non-credential var must never be redacted."""
    monkeypatch.setenv("PROJECT_PATH_HINT", "ordinary-value-1234")
    assert se._redact_secrets("ordinary-value-1234") == "ordinary-value-1234"


def test_unit_cr_secret_masked(monkeypatch):
    """Unit-level: the redactor masks the universal-newline form."""
    monkeypatch.setenv("CI_DEPLOY_KEY", "UNITLEFT0000\r\nUNITRIGHT1111")
    out = se._redact_secrets("UNITLEFT0000\nUNITRIGHT1111")
    assert "UNITLEFT0000" not in out
    assert "UNITRIGHT1111" not in out
