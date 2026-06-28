"""Round-31 env red-team: NON-arm leak vectors + a descriptive-suffix NAME gap.

Provider-token VALUE arms are exhausted (rounds 26-30), so round 31 pivots to
the two classes the brief calls out:

  (A) Leak CHANNELS other than the stdout/stderr redaction path -- verify
      ``_redact_secrets`` is actually applied on EVERY sink that persists
      script-derived data (the command string logged on timeout/error, the
      exception ``str(exc)`` field, the HTTP ``safe_url`` target).  These are
      probed and found to HOLD (CLEAN controls below).

  (B) NAME-denylist GAPS: a secret in an env var whose NAME APM has ALREADY
      decided is a credential -- the token (``AUTHORIZATION`` / ``SECRET`` /
      ``TOKEN``) is literally in ``_CREDENTIAL_DENYLIST`` -- but where a benign
      descriptive SUFFIX (``_HEADER`` / ``_VALUE``) sits after the token and
      defeats the regex end-anchor, so the value is neither stripped from the
      child env NOR masked in ``~/.apm/logs/scripts.log``.

GENUINE BREAK (B): ``_CREDENTIAL_DENYLIST`` ends each token with
``...[_0-9]*$``.  The token must be at (or near) the END of the name.  A
trailing descriptive word the authors did NOT enumerate -- ``_HEADER`` after
``AUTHORIZATION``, ``_VALUE`` after ``SECRET``/``TOKEN`` -- pushes the token
off the anchor, so ``_matches_credential`` returns False even though the SAME
token at the end (``PROXY_AUTHORIZATION``, ``CLIENT_SECRET``, ``API_TOKEN``) is
masked.  Concretely:

  * ``AUTHORIZATION_HEADER`` / ``AUTH_HEADER`` -- the value is, by definition,
    an HTTP Authorization header (``Basic base64(user:pass)`` or an opaque
    bearer).  A ``Basic`` header carries NO structural anchor (no ``eyJ.eyJ``
    JWT, no ``user:pass@`` URL, no ``password=`` DSN), so when a lifecycle
    script echoes ``$AUTHORIZATION_HEADER`` (a ``curl -H "$AUTHORIZATION_HEADER"``
    debug ``set -x`` trace is the canonical case) the live credential persists
    in cleartext in the 0600 audit log.  ``AUTHORIZATION`` is already a
    denylist token -- this is an internal inconsistency, not a scope decision.
    The module's own comment (the JWT-value masker docstring) names
    ``AUTH_HEADER`` as a benign-NAMED carrier and relies on JWT VALUE masking
    to cover it -- which silently fails for a non-JWT ``Basic`` / opaque value.

  * ``SECRET_VALUE`` / ``TOKEN_VALUE`` -- ``SECRET`` and ``TOKEN`` are denylist
    tokens; the trailing ``_VALUE`` defeats the anchor the same way.

SECONDARY value-arm (B'): the ``age`` / ``sops`` secret key
``AGE-SECRET-KEY-1...`` is printed to stdout by ``age-keygen`` in a secrets-
decryption deploy lifecycle and is matched by no NAME and no VALUE masker.  Its
``AGE-SECRET-KEY-1`` prefix is a rigid zero-false-positive anchor.

Every secret-shaped literal is assembled at RUNTIME from fragments so GitHub
push-protection cannot block the probe.  The genuine-break tests assert the
SECURE contract (the credential is ABSENT from the real 0600 scripts.log /
redaction output) and therefore FAIL on HEAD b58b969c2 (red-before-confirmed),
and would PASS once the suffix gap / value arm is closed.  The CLEAN-control
tests assert a channel HOLDS and PASS on HEAD.  Benign look-alike SURVIVAL
guards accompany each proposed masking change.
"""

from __future__ import annotations

import base64
import os
import secrets
import string
import threading

import pytest

from apm_cli.core import script_executors
from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.core.script_executors import (
    _append_to_script_log,
    _build_script_env,
    _matches_credential,
    _redact_secrets,
)


def _watchdog(seconds: float = 20.0):
    """Fail fast on a hang without using a shell timeout (banned)."""

    def _boom():  # pragma: no cover - only fires on a genuine hang
        os._exit(99)

    t = threading.Timer(seconds, _boom)
    t.daemon = True
    t.start()
    return t


def _opaque(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _basic_auth_header() -> str:
    """A real HTTP ``Basic`` Authorization header value (base64 user:pass).

    No JWT, no ``user:pass@`` URL, no ``password=`` DSN -- so NO structural
    value masker fires; only the NAME can signal it is a credential.
    """
    creds = f"deploybot:{_opaque(20)}".encode()
    return "Basic " + base64.b64encode(creds).decode()


def _read_log() -> str:
    log_path = script_executors._get_scripts_log_path()
    return log_path.read_text(encoding="utf-8")


# ===========================================================================
# Finding r31-env-1 (MED): a benign descriptive suffix (_HEADER / _VALUE)
# defeats an ALREADY-denylisted credential token, leaking the value.
# RED-BEFORE: these FAIL on HEAD (the credential survives redaction).
# ===========================================================================


@pytest.mark.parametrize("name", ["AUTHORIZATION_HEADER", "AUTH_HEADER"])
def test_round31_auth_header_value_redacted_from_output(name):
    """A ``Basic`` Authorization-header env value must not survive redaction."""
    wd = _watchdog()
    try:
        value = _basic_auth_header()
        os.environ[name] = value
        try:
            redacted = _redact_secrets(f"+ curl -H {name}={value} https://api.example")
        finally:
            os.environ.pop(name, None)
        assert value not in redacted, (
            f"{name} Authorization-header credential leaked through "
            "_redact_secrets: the token AUTHORIZATION is already denylisted but "
            "the trailing _HEADER defeats the end-anchor, so the live header "
            "value (Basic base64(user:pass)) persists in cleartext"
        )
    finally:
        wd.cancel()


def test_round31_auth_header_value_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real 0600 writer must not persist an Authorization header."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        value = _basic_auth_header()
        monkeypatch.setenv("AUTHORIZATION_HEADER", value)

        # A `set -x` debug trace of a curl invocation -- the canonical accidental
        # echo of an auth-header env var into captured stdout.
        _append_to_script_log(
            "post-install",
            "command",
            'curl -fsSL -H "$AUTHORIZATION_HEADER" https://api.example/ping',
            stdout=f"+ curl -fsSL -H AUTHORIZATION_HEADER={value} https://api.example/ping\n",
            status="ok",
            exit_code=0,
        )
        contents = _read_log()
        assert value not in contents, (
            "AUTHORIZATION_HEADER credential persisted in cleartext in the 0600 "
            f"scripts.log (the claimed 'credential values redacted' guarantee); "
            f"leak at {script_executors._get_scripts_log_path()}"
        )
    finally:
        wd.cancel()


@pytest.mark.parametrize("name", ["SECRET_VALUE", "TOKEN_VALUE"])
def test_round31_secret_value_suffix_redacted_from_output(name):
    """SECRET_VALUE / TOKEN_VALUE: trailing _VALUE defeats the SECRET/TOKEN token."""
    wd = _watchdog()
    try:
        value = "opaque_" + _opaque(34)
        os.environ[name] = value
        try:
            redacted = _redact_secrets(f"resolved {name} -> {value}")
        finally:
            os.environ.pop(name, None)
        assert value not in redacted, (
            f"{name} leaked: the {name.split('_')[0]} token is denylisted yet the "
            "trailing _VALUE pushes it off the end-anchor, so the secret survives"
        )
    finally:
        wd.cancel()


def test_round31_auth_header_propagates_to_child_env():
    """Corroboration: the un-masked auth-header name also reaches the child env.

    ``_build_script_env`` strips credential-named vars from a command script's
    environment.  Because ``AUTHORIZATION_HEADER`` is not recognised, the live
    header is handed to a (potentially hostile) lifecycle subprocess intact.
    """
    wd = _watchdog()
    try:
        value = _basic_auth_header()
        os.environ["AUTHORIZATION_HEADER"] = value
        try:
            script = ScriptEntry(script_type="command", event="post-install")
            child_env = _build_script_env(script)
        finally:
            os.environ.pop("AUTHORIZATION_HEADER", None)
        assert child_env.get("AUTHORIZATION_HEADER") != value, (
            "AUTHORIZATION_HEADER (a live Authorization header) was propagated "
            "verbatim into the command-script child environment; a sibling "
            "PROXY_AUTHORIZATION is correctly stripped"
        )
    finally:
        wd.cancel()


# ===========================================================================
# Finding r31-env-2 (LOW/MED): age / sops secret key VALUE arm.
# RED-BEFORE: FAILS on HEAD (the key survives), PASSES once an arm is added.
# ===========================================================================


def _age_secret_key() -> str:
    body = "".join(secrets.choice("0123456789abcdefghjkmnpqrstvwxyz") for _ in range(58))
    return "AGE-SECRET-KEY-1" + body.upper()


def test_round31_age_secret_key_redacted_from_output():
    """``age-keygen`` prints AGE-SECRET-KEY-1... to stdout; it must be masked."""
    wd = _watchdog()
    try:
        key = _age_secret_key()
        stdout = "# created: 2026-06-28T00:00:00Z\n# public key: age1qqqq...\n" + key + "\n"
        redacted = _redact_secrets(stdout)
        assert key not in redacted, (
            "age/sops secret key (AGE-SECRET-KEY-1...) leaked through "
            "_redact_secrets: no NAME and no VALUE masker covers it, yet its "
            "fixed prefix is a rigid zero-FP anchor"
        )
    finally:
        wd.cancel()


def test_round31_age_secret_key_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: an age secret key must not persist in scripts.log."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        key = _age_secret_key()
        _append_to_script_log(
            "post-install",
            "command",
            "age-keygen",
            stdout=key + "\n",
            status="ok",
            exit_code=0,
        )
        assert key not in _read_log(), "age secret key persisted in cleartext in scripts.log"
    finally:
        wd.cancel()


# ===========================================================================
# False-positive SURVIVAL guards for the proposed maskers.
# These PASS on HEAD (nothing masks them) and MUST keep passing after the fix:
# a masker that corrupts benign output is itself a bug.
# ===========================================================================


@pytest.mark.parametrize(
    "benign_name",
    [
        "CONTENT_TYPE_HEADER",  # _HEADER but no credential token
        "X_FORWARDED_HEADER",
        "REQUEST_HEADER_NAME",
        "RESPONSE_HEADER_LIMIT",
        "MAX_VALUE",  # _VALUE but no credential token
        "DEFAULT_VALUE",
        "ENV_VALUE",
        "RETURN_VALUE",
    ],
)
def test_round31_benign_header_value_names_not_credentials(benign_name):
    """Names with a _HEADER/_VALUE tail but NO credential token stay non-secret.

    The proposed fix only allows the suffix AFTER a credential token, so these
    must continue to reach the child env / log unredacted.
    """
    assert not _matches_credential(benign_name), (
        f"{benign_name} must NOT be treated as a credential; it carries no "
        "AUTHORIZATION/SECRET/TOKEN token"
    )


@pytest.mark.parametrize(
    "benign_value",
    [
        "AGE-RECIPIENTS=age1qqqqqqqqqqqqqqqqqqqqqqqq",  # public recipient, not a key
        "manage_secret_keys_rotation_job",  # 'age-secret-key' words, no prefix
        "AGE-SECRET-KEY-MISSING",  # right prefix, no 58-char bech32 body
    ],
)
def test_round31_age_lookalikes_survive(benign_value):
    """Benign strings near the age prefix must survive any new value arm."""
    wd = _watchdog()
    try:
        assert _redact_secrets(benign_value) == benign_value, (
            f"benign look-alike was over-redacted: {benign_value!r}"
        )
    finally:
        wd.cancel()


# ===========================================================================
# CLEAN CONTROLS: channels the brief asked to verify -- _redact_secrets IS
# applied, so these PASS on HEAD.  They document the budget and prove the
# break above is specific to the NAME gap, not a channel-wide failure.
# ===========================================================================


def test_round31_control_proxy_authorization_is_masked():
    """Control: the SAME Basic header under a recognised NAME IS masked."""
    wd = _watchdog()
    try:
        value = _basic_auth_header()
        for name in ("PROXY_AUTHORIZATION", "AUTHORIZATION", "CLIENT_SECRET", "API_TOKEN"):
            os.environ[name] = value
            try:
                assert value not in _redact_secrets(f"{name}={value}"), (
                    f"{name} unexpectedly leaked -- control should be masked"
                )
            finally:
                os.environ.pop(name, None)
    finally:
        wd.cancel()


def test_round31_control_command_string_redacted_on_error(tmp_path, monkeypatch):
    """Channel: the command-string target and str(exc) stderr ARE redacted.

    A denylisted env value embedded in the logged command / exception must not
    persist -- verifies the error-path sink runs _redact_secrets (it does).
    """
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        secret = "secretvalue_" + _opaque(30)
        monkeypatch.setenv("DEPLOY_TOKEN", secret)  # TOKEN -> recognised
        _append_to_script_log(
            "post-install",
            "command",
            f"deploy --token {secret}",  # secret in the command (target) field
            stderr=f"boom: token {secret} rejected",  # secret in str(exc)-style stderr
            status="error",
            exit_code=1,
        )
        contents = _read_log()
        assert secret not in contents, "recognised token leaked via command/stderr log fields"
    finally:
        wd.cancel()


def test_round31_control_http_target_provider_token_redacted(tmp_path, monkeypatch):
    """Channel: an HTTP target URL carrying a provider token IS redacted in the log."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        ghp = "ghp_" + _opaque(36)
        _append_to_script_log(
            "post-install",
            "http",
            f"https://hooks.example.com/notify?token={ghp}",
            stdout="HTTP 200",
            status="ok",
        )
        assert ghp not in _read_log(), "provider token in HTTP target URL leaked to scripts.log"
    finally:
        wd.cancel()
