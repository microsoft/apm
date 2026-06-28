"""Round-27 red-team probe (ENV / SECRET-EXFIL): the ``*_COOKIE`` credential-NAME family.

Earlier rounds closed the suffix-token denylist (TOKEN / SECRET / KEY / PAT /
PASS* / PWD / CREDENTIAL / AUTH* / JWT / MNEMONIC / SEED_PHRASE ...) plus the
curated secret-manager SESSION names (BW_SESSION / OP_SESSION / FASTLANE_SESSION,
round-23). One real-world credential-NAME family is still entirely uncovered:

  r27-env-1 (session-COOKIE bearer family): a session / authentication COOKIE is a
    bearer credential -- whoever holds the cookie value is authenticated as the
    user for the cookie's lifetime, exactly like a session token. CI/test/scraping
    tooling routinely stores it in an env var whose NAME ends in ``_COOKIE`` and
    contains NONE of the denylist tokens:

      * ``SESSION_COOKIE``  -- a web-session bearer (Django/Flask/Rails sessionid,
                               a scraped auth cookie a Playwright/Selenium test
                               replays).
      * ``AUTH_COOKIE``     -- an authentication cookie a CI step injects to call
                               an authenticated endpoint.

    ``COOKIE`` is NOT a denylist token, is not a curated blob name, and the cookie
    VALUE is an opaque base64-ish blob: no ``=`` key, no ``scheme://user:pass@``
    URL, no PEM armor, no ``eyJ.eyJ`` JWT shape, no ``sig=`` SAS token, no
    fixed provider prefix -- so NONE of the structural value-maskers fire either.

    The result is a DOUBLE-sink break on the DEFAULT path, no opt-in, no warning:
      (a) the cookie bearer STAYS in the child process environment of every
          command-type lifecycle script (``_build_script_env`` does not strip it),
          so a malicious dependency post-install script reads ``$SESSION_COOKIE``
          and exfiltrates it; and
      (c) ``$SESSION_COOKIE`` referenced in an http-event HEADER value is EXPANDED
          and sent to the configured endpoint, with NO ``credential variable will
          NOT be expanded`` warning (``_is_denylisted`` -> ``_matches_credential``
          returns False).

  The fix is surgical and over-strip-safe: a ``(?:^|_)COOKIES?$`` suffix-anchored
  token (or exact ``COOKIE``/``SESSION_COOKIE``/``AUTH_COOKIE`` blob names). It
  leaves the benign cookie *config* a script legitimately reads -- ``COOKIE_DOMAIN``
  / ``COOKIE_NAME`` / ``COOKIE_PATH`` / ``COOKIE_SECURE`` (COOKIE is a PREFIX, not
  the suffix) -- untouched in the child env, as the control test below proves.

A genuine break = the SECURE assertion (cookie ABSENT from child env / NOT
expanded into the header) FAILS against current code.

Cookie-shaped values are assembled at RUNTIME from a benign prefix so nothing
here resembles a real scannable secret (push-protection).
"""

from __future__ import annotations

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


def _fake_cookie_bearer() -> str:
    # An opaque session-cookie value (no token marker, no structural shape).
    return "r27FAKE" + "sessionCOOKIEbearer" + "x" * 24


# --- Sink (a): child process environment -----------------------------------


def test_session_cookie_stripped_from_child_env(monkeypatch):
    """A ``SESSION_COOKIE`` bearer must NOT be inherited by a command script's
    child environment -- it is a credential, not benign config."""
    secret = _fake_cookie_bearer()
    monkeypatch.setenv("SESSION_COOKIE", secret)
    script = ScriptEntry(script_type="command", event="post-install", bash="env")
    env = se._build_script_env(script)
    assert "SESSION_COOKIE" not in env, (
        "session-cookie bearer leaked into the child process environment of a "
        "lifecycle script (any subprocess, incl. a malicious dependency, can read it)"
    )


def test_auth_cookie_stripped_from_child_env(monkeypatch):
    """An ``AUTH_COOKIE`` authentication bearer must not reach the child env."""
    secret = _fake_cookie_bearer()
    monkeypatch.setenv("AUTH_COOKIE", secret)
    script = ScriptEntry(script_type="command", event="pre-install", bash="env")
    env = se._build_script_env(script)
    assert "AUTH_COOKIE" not in env, "auth-cookie bearer leaked into the child env"


# --- Sink (c): HTTP-event header $VAR expansion -----------------------------


def test_session_cookie_blocked_in_header_expansion(monkeypatch):
    """``$SESSION_COOKIE`` referenced in an http header value must be BLOCKED
    (expanded to empty), not silently sent to the remote endpoint."""
    secret = _fake_cookie_bearer()
    monkeypatch.setenv("SESSION_COOKIE", secret)
    expanded = se._expand_env_vars("Bearer $SESSION_COOKIE", frozenset())
    assert secret not in expanded, (
        "session-cookie bearer was expanded into an outbound HTTP header value and "
        "would be sent to the configured endpoint with no warning"
    )


def test_auth_cookie_blocked_in_braced_header_expansion(monkeypatch):
    """The ``${AUTH_COOKIE}`` braced form must be blocked the same way."""
    secret = _fake_cookie_bearer()
    monkeypatch.setenv("AUTH_COOKIE", secret)
    expanded = se._expand_env_vars("cookie=${AUTH_COOKIE}", frozenset())
    assert secret not in expanded, "auth-cookie bearer expanded into header via braced form"


# --- Controls: benign cookie CONFIG must be preserved (no over-strip) -------


def test_benign_cookie_config_preserved_in_child_env(monkeypatch):
    """A surgical fix must NOT sweep benign cookie *config* (COOKIE is a prefix,
    not the suffix) out of the child env."""
    monkeypatch.setenv("COOKIE_DOMAIN", "example.com")
    monkeypatch.setenv("COOKIE_NAME", "sid")
    monkeypatch.setenv("COOKIE_PATH", "/")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    script = ScriptEntry(script_type="command", event="post-install", bash="env")
    env = se._build_script_env(script)
    for name in ("COOKIE_DOMAIN", "COOKIE_NAME", "COOKIE_PATH", "COOKIE_SECURE"):
        assert name in env, f"benign cookie config {name} was over-stripped from the child env"


def test_allowed_env_vars_opt_in_still_works(monkeypatch):
    """The opt-in escape hatch must keep working: a script that explicitly
    allowlists SESSION_COOKIE may still expand it (no false block after the fix)."""
    secret = _fake_cookie_bearer()
    monkeypatch.setenv("SESSION_COOKIE", secret)
    expanded = se._expand_env_vars("Bearer $SESSION_COOKIE", frozenset({"SESSION_COOKIE"}))
    assert secret in expanded, (
        "explicit allowedEnvVars opt-in for SESSION_COOKIE was wrongly blocked"
    )
