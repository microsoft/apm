"""Round-22 red-team probes for the ENV-EXFIL surface.

Drives the REAL credential-matching, child-env filtering, log redaction, and
``$VAR`` header-expansion code in ``script_executors``. The round-19/20/21 work
added an ENCODING-tail alternation
``(?:_?(?:BASE64|BASE32|B64|B32|HEX|PEM|DER|ASC))?`` to the suffix denylist so a
secret that CI flattens into ONE single-line var named ``<token>_BASE64`` keeps
matching even though the credential token is now an INFIX. Two sibling tails of
the SAME convention were missed and produce genuine default-path breaks:

  r22-env-1 (serialization-format tail gap): the ubiquitous way a whole
    structured credential is inlined into one env var is NOT base64 -- it is the
    raw serialization itself: ``GOOGLE_CREDENTIALS_JSON``, ``GCP_SA_KEY_JSON``,
    ``SERVICE_ACCOUNT_KEY_JSON`` (the GCP service-account secret terraform /
    gcloud read verbatim), plus ``*_SECRET_JSON`` / ``*_PASSWORD_JSON`` /
    ``*_CREDENTIALS_YAML`` / ``*_KEY_TOML``. The credential token (KEY / SECRET /
    PASSWORD / CREDENTIAL) is an infix and the name ENDS in the benign
    ``_JSON`` / ``_YAML`` / ``_TOML`` format tail, which the encoding alternation
    does not list. So the value (a) stays in the child env, (b) leaks cleartext
    to the 0600 scripts.log, and (c) expands into an outbound HTTP header -- all
    on the DEFAULT path, no opt-in, no warning.

  r22-env-2 (crypto / binary base-encoding tail gap): the encoding alternation
    covers BASE64/BASE32/HEX but omits the base encodings that web3 / signing /
    binary-safe pipelines actually use to flatten a key into one var:
    ``SOLANA_PRIVATE_KEY_BASE58`` (the canonical Solana/Bitcoin key encoding),
    ``SIGNING_KEY_ASCII85`` / ``CERT_KEY_Z85`` (binary-safe ASCII85/Z85),
    ``API_TOKEN_URLSAFE`` (base64url), ``WALLET_SECRET_BASE62``. Same infix-token
    structure, same three-sink leak.

Secret-looking literals are assembled at runtime from fragments (scan-safe).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


def _secretval(tag: str) -> str:
    # Long, structureless, no '=', no URL, no PEM armor -- a pure exact-value
    # needle so any hit in env/log/header is unambiguously THIS secret.
    return tag + "_" + "A1b2C3d4" + "E5f6G7h8" + "I9j0K1l2" + "M3n4O5p6"


def _cmd_entry(allowed=None) -> ScriptEntry:
    return ScriptEntry(
        script_type="command",
        event="post-install",
        bash="env",
        allowed_env_vars=allowed,
    )


# --------------------------------------------------------------------------- #
# r22-env-1 -- serialization-format tail (_JSON / _YAML / _TOML)              #
# --------------------------------------------------------------------------- #

SERIAL_NAMES = [
    "GOOGLE_CREDENTIALS_JSON",
    "GCP_SA_KEY_JSON",
    "SERVICE_ACCOUNT_KEY_JSON",
    "API_SECRET_JSON",
    "DB_PASSWORD_JSON",
    "GCP_CREDENTIALS_YAML",
    "SIGNING_KEY_TOML",
]


@pytest.mark.parametrize("name", SERIAL_NAMES)
def test_r22_env_1_serialization_recognised(name: str) -> None:
    """A ``<token>_JSON`` / ``_YAML`` / ``_TOML`` inlined secret must match."""
    assert se._matches_credential(name) is True, f"{name} escaped the denylist"


@pytest.mark.parametrize("name", SERIAL_NAMES)
def test_r22_env_1_serialization_stripped_from_child_env(name: str, monkeypatch) -> None:
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) != val, f"{name} leaked into the child env"


@pytest.mark.parametrize("name", SERIAL_NAMES)
def test_r22_env_1_serialization_redacted_in_log(name: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    se._append_to_script_log("post-install", "command", "echo x", stdout=f"{name}={val}")
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert val not in log, f"{name} value leaked to scripts.log"


def test_r22_env_1_json_credential_refused_in_header(monkeypatch) -> None:
    """A ``${GOOGLE_CREDENTIALS_JSON}`` reference must NOT expand into a header."""
    name = "GOOGLE_CREDENTIALS_JSON"
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    expanded = se._expand_env_vars(f"X-Auth: ${{{name}}}", frozenset())
    assert val not in expanded, "JSON-tail credential expanded into an HTTP header"


# --------------------------------------------------------------------------- #
# r22-env-2 -- crypto / binary base-encoding tails (BASE58/ASCII85/Z85/...)   #
# --------------------------------------------------------------------------- #

ENCODING_NAMES = [
    "SOLANA_PRIVATE_KEY_BASE58",
    "SIGNING_KEY_ASCII85",
    "CERT_KEY_Z85",
    "API_TOKEN_URLSAFE",
    "WALLET_SECRET_BASE62",
]


@pytest.mark.parametrize("name", ENCODING_NAMES)
def test_r22_env_2_encoding_recognised(name: str) -> None:
    assert se._matches_credential(name) is True, f"{name} escaped the denylist"


@pytest.mark.parametrize("name", ENCODING_NAMES)
def test_r22_env_2_encoding_stripped_from_child_env(name: str, monkeypatch) -> None:
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) != val, f"{name} leaked into the child env"


@pytest.mark.parametrize("name", ENCODING_NAMES)
def test_r22_env_2_encoding_redacted_in_log(name: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    se._append_to_script_log("post-install", "command", "echo x", stdout=f"{name}={val}")
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert val not in log, f"{name} value leaked to scripts.log"


def test_r22_env_2_base58_refused_in_header(monkeypatch) -> None:
    name = "SOLANA_PRIVATE_KEY_BASE58"
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    expanded = se._expand_env_vars(f"Bearer ${{{name}}}", frozenset())
    assert val not in expanded, "BASE58 key expanded into an HTTP header value"
