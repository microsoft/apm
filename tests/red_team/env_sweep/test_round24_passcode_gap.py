"""Round-24 red-team probes: credential-name gaps in the lifecycle env surface.

Each probe drives the REAL functions in script_executors.py and asserts the
SECURE outcome (secret stripped from child env, refused for header expansion,
masked in scripts.log). A probe that FAILS at head = a genuine leak.

Secret-looking values are assembled at runtime from a benign prefix + a short
fake token so nothing here resembles a real scannable secret.
"""

from __future__ import annotations

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


def _fake_secret() -> str:
    # >= _MIN_REDACT_LEN so the value is eligible for log substring masking.
    return "pc-" + "FAKE" + "00991234"


def _make_cmd(name: str) -> ScriptEntry:
    return ScriptEntry(script_type="command", event="post-install", bash="env")


# -- PASSCODE: a PASS-family secret the (?:^|_)PASS anchor misses (CODE blocks
#    the end-anchor and there is no dedicated PASSCODE token) ---------------


def test_passcode_stripped_from_child_env(monkeypatch):
    monkeypatch.setenv("MFA_PASSCODE", _fake_secret())
    env = se._build_script_env(_make_cmd("MFA_PASSCODE"))
    assert "MFA_PASSCODE" not in env, "PASSCODE secret leaked into child env"


def test_passcode_refused_for_header_expansion(monkeypatch):
    secret = _fake_secret()
    monkeypatch.setenv("MFA_PASSCODE", secret)
    out = se._expand_env_vars("Bearer $MFA_PASSCODE", frozenset())
    assert secret not in out, "PASSCODE secret expanded into outbound header"


def test_passcode_masked_in_scripts_log(monkeypatch, tmp_path):
    secret = _fake_secret()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("MFA_PASSCODE", secret)
    se._append_to_script_log(
        "post-install",
        "command",
        "env",
        stdout=f"MFA_PASSCODE={secret}",
    )
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert secret not in log, "PASSCODE secret persisted to scripts.log in cleartext"


# -- JWT / BEARER: bare modern-bearer names whose value (eyJ... / opaque) is
#    caught by no structural masker --------------------------------------


def _fake_jwt() -> str:
    return "eyJ" + "FAKEHEADER" + ".eyJ" + "FAKEBODY" + ".sig" + "FAKE0099"


def test_jwt_masked_in_scripts_log(monkeypatch, tmp_path):
    secret = _fake_jwt()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("JWT", secret)
    se._append_to_script_log(
        "post-install",
        "command",
        "env",
        stdout=f"JWT={secret}",
    )
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert secret not in log, "JWT bearer leaked to scripts.log in cleartext"


def test_jwt_stripped_from_child_env(monkeypatch):
    monkeypatch.setenv("JWT", _fake_jwt())
    env = se._build_script_env(_make_cmd("JWT"))
    assert "JWT" not in env, "JWT bearer leaked into child env"
