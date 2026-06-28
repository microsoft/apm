"""Round-21 red-team probes for the ENV-EXFIL surface.

Drives the REAL credential-matching, child-env filtering, log redaction, and
$VAR header-expansion code in ``script_executors``. Two genuine default-path
breaks are isolated:

  r21-env-1 (encoding-tail gap): the round-19 encoding-tail
    ``(?:_?(?:BASE64|B64|HEX|PEM|DER|ASC))?`` lets a secret keep matching when CI
    encodes it into one var, BUT omits BASE32 / B32 -- the canonical TOTP/MFA/HOTP
    shared-secret encoding (RFC 6238). Appending ``_BASE32`` to an otherwise
    caught name (``TOTP_SECRET`` -> ``TOTP_SECRET_BASE32``, ``API_TOKEN`` ->
    ``API_TOKEN_B32``) makes the credential token an INFIX with an uncovered tail,
    so the value (a) stays in the child env and (b) leaks cleartext to the 0600
    scripts.log and (c) expands into an outbound HTTP header -- all on the DEFAULT
    path, no opt-in.

  r21-env-2 (Authorization-name gap): ``AUTHORIZATION`` / ``HTTP_AUTHORIZATION``
    / ``PROXY_AUTHORIZATION`` literally name an HTTP credential, yet contain no
    denylist token (AUTH alone is not a token; AUTHTOKEN is), so they fully
    escape the denylist on the default path.

Secret-looking literals are assembled at runtime from fragments (scan-safe).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


def _secretval(tag: str) -> str:
    return tag + "_" + "A1b2C3d4" + "E5f6G7h8" + "I9j0K1l2" + "M3n4O5p6"


def _cmd_entry(allowed=None) -> ScriptEntry:
    return ScriptEntry(
        script_type="command",
        event="post-install",
        bash="env",
        allowed_env_vars=allowed,
    )


# --------------------------------------------------------------------------- #
# r21-env-1 -- BASE32 / B32 encoding-tail gap (TOTP/MFA secret convention)     #
# --------------------------------------------------------------------------- #

BASE32_NAMES = ["TOTP_SECRET_BASE32", "MFA_KEY_BASE32", "API_TOKEN_B32", "HOTP_SECRET_B32"]


@pytest.mark.parametrize("name", BASE32_NAMES)
def test_r21_env_1_base32_recognised(name: str) -> None:
    """A ``<token>_BASE32`` / ``_B32`` encoded secret must be a credential."""
    assert se._matches_credential(name) is True, f"{name} escaped the denylist"


@pytest.mark.parametrize("name", BASE32_NAMES)
def test_r21_env_1_base32_stripped_from_child_env(name: str, monkeypatch) -> None:
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) != val, f"{name} leaked into the child env"


@pytest.mark.parametrize("name", BASE32_NAMES)
def test_r21_env_1_base32_redacted_in_log(name: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    se._append_to_script_log("post-install", "command", "echo x", stdout=f"{name}={val}")
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert val not in log, f"{name} value leaked to scripts.log"


def test_r21_env_1_base32_refused_in_header(monkeypatch) -> None:
    """A ``${<token>_BASE32}`` reference must NOT expand into an HTTP header."""
    name = "TOTP_SECRET_BASE32"
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    expanded = se._expand_env_vars(f"Bearer ${{{name}}}", frozenset())
    assert val not in expanded, "BASE32 secret expanded into an HTTP header value"


# --------------------------------------------------------------------------- #
# r21-env-2 -- AUTHORIZATION family (HTTP credential names, no denylist token) #
# --------------------------------------------------------------------------- #

AUTHZ_NAMES = ["AUTHORIZATION", "HTTP_AUTHORIZATION", "PROXY_AUTHORIZATION"]


@pytest.mark.parametrize("name", AUTHZ_NAMES)
def test_r21_env_2_authorization_recognised(name: str) -> None:
    assert se._matches_credential(name) is True, f"{name} escaped the denylist"


@pytest.mark.parametrize("name", AUTHZ_NAMES)
def test_r21_env_2_authorization_stripped_from_child_env(name: str, monkeypatch) -> None:
    val = "Bearer " + _secretval(name)
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) != val, f"{name} bearer leaked into the child env"


@pytest.mark.parametrize("name", AUTHZ_NAMES)
def test_r21_env_2_authorization_redacted_in_log(name: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    secret = _secretval(name)
    val = "Bearer " + secret
    monkeypatch.setenv(name, val)
    se._append_to_script_log("post-install", "command", "echo x", stdout=f"{name}={val}")
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert secret not in log, f"{name} bearer leaked to scripts.log"
