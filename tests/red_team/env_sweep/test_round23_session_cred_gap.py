"""Round-23 red-team probe for the ENV-EXFIL surface.

Earlier rounds closed the suffix-token denylist (TOKEN / SECRET / KEY / PAT /
PASS* / PWD / CREDENTIAL / AUTH* / MNEMONIC / SEED_PHRASE) plus its rotation,
``V<n>``, serialization (_JSON/_YAML/_TOML) and base-encoding tails, and the
curated blob names (DOCKER_AUTH_CONFIG, WALLET_SEED, ...). One real-world
credential-NAME family is still entirely uncovered:

  r23-env-1 (secret-manager CLI SESSION-key family): the password managers whose
    CLI is the canonical way a build authenticates carry their unlock secret in a
    ``*_SESSION`` env var whose NAME contains NONE of the denylist tokens:

      * ``BW_SESSION``        -- the Bitwarden CLI session key. It is the master
                                 decryption key for the WHOLE vault; ``bw`` reads
                                 it from this env var (``export BW_SESSION=...``).
      * ``FASTLANE_SESSION``  -- the Apple Developer Portal session cookie used by
                                 fastlane in CI to publish to TestFlight / the App
                                 Store. Whoever holds it can ship apps as you.
      * ``OP_SESSION`` / ``OP_SESSION_<account>`` -- the 1Password CLI (v1) session
                                 token.

    ``SESSION`` is NOT a denylist token (and cannot become a bare one without
    sweeping the benign ``SESSION_ID`` / ``SESSION_TIMEOUT`` / ``PHP_SESSION`` /
    ``RAILS_SESSION_KEY`` config a script legitimately reads). The value is an
    opaque base64-ish blob: no ``=`` key, no ``scheme://user:pass@`` URL, no PEM
    armor, no ``sig=`` SAS token -- so NONE of the structural value-maskers
    (``_redact_connection_string_password`` / ``_redact_embedded_url_credentials``
    / ``_redact_webhook_urls`` / ``_redact_sas_signatures`` /
    ``_redact_pem_private_keys``) fire either. The result is a three-sink break on
    the DEFAULT path, no opt-in, no warning: the vault key (a) stays in the child
    env, (b) leaks cleartext into the 0600 scripts.log when a script echoes its
    environment, and (c) expands verbatim into an outbound HTTP header.

The fix mirrors the existing curated-name pattern: add the exact names to
``_CREDENTIAL_BLOB_NAMES`` and an ``OP_SESSION_`` arm to ``_CREDENTIAL_NAME_PREFIX``
(1Password names per-account sessions ``OP_SESSION_<uuid>``), so the wallet key is
masked without colliding with the benign ``*_SESSION`` config siblings.

Secret-looking literals are assembled at runtime from fragments (scan-safe).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


def _secretval(tag: str) -> str:
    # Long, structureless, no '=', no URL, no PEM armor -- a pure exact-value
    # needle so any hit in env/log/header is unambiguously THIS secret.
    return tag[:4] + "_" + "Q7w8E9r0" + "T1y2U3i4" + "O5p6A7s8" + "D9f0G1h2"


def _cmd_entry(allowed=None) -> ScriptEntry:
    return ScriptEntry(
        script_type="command",
        event="post-install",
        bash="env",
        allowed_env_vars=allowed,
    )


# The secret-manager CLI session-key family. Opaque values, no denylist token.
SESSION_NAMES = [
    "BW_SESSION",  # Bitwarden CLI vault master key
    "FASTLANE_SESSION",  # Apple Dev Portal session cookie
    "OP_SESSION",  # 1Password CLI session
    "OP_SESSION_MY_ACCOUNT",  # 1Password per-account session (real form)
]


# Benign siblings that MUST keep reaching the child env (regression guard for
# the fix: a bare ``SESSION`` token would wrongly sweep these).
BENIGN_SESSION_NAMES = [
    "SESSION_ID",
    "SESSION_TIMEOUT",
    "PHP_SESSION",
    "DJANGO_SESSION_ENGINE",
]


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_r23_env_1_session_recognised(name: str) -> None:
    """A secret-manager ``*_SESSION`` unlock key must match the credential set."""
    assert se._matches_credential(name) is True, f"{name} escaped the denylist"


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_r23_env_1_session_stripped_from_child_env(name: str, monkeypatch) -> None:
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) != val, f"{name} leaked into the child env"


@pytest.mark.parametrize("name", SESSION_NAMES)
def test_r23_env_1_session_redacted_in_log(name: str, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    se._append_to_script_log("post-install", "command", "echo x", stdout=f"{name}={val}")
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert val not in log, f"{name} value leaked cleartext to scripts.log"


def test_r23_env_1_session_refused_in_header(monkeypatch) -> None:
    """A ``${BW_SESSION}`` reference must NOT expand into an HTTP header value."""
    name = "BW_SESSION"
    val = _secretval(name)
    monkeypatch.setenv(name, val)
    expanded = se._expand_env_vars(f"X-Vault: ${{{name}}}", frozenset())
    assert val not in expanded, "BW_SESSION vault key expanded into an HTTP header"


@pytest.mark.parametrize("name", BENIGN_SESSION_NAMES)
def test_r23_env_1_benign_session_preserved(name: str, monkeypatch) -> None:
    """Non-secret ``*_SESSION`` config must still reach the child env intact."""
    val = "benign-session-config-value-1234"
    monkeypatch.setenv(name, val)
    env = se._build_script_env(_cmd_entry())
    assert env.get(name) == val, f"{name} was wrongly stripped (over-redaction)"
