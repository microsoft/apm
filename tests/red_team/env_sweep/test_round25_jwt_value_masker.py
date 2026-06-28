"""Round-25 red-team probes: structural JWT VALUE leak in scripts.log.

Round-24 added NAME coverage for ``*_JWT`` vars, but the module comment at
``script_executors.py`` is explicit that "The ``eyJ...`` value matches no
structural masker, so a bare *_JWT name is the only signal." That leaves two
genuine under-redaction paths the name-based denylist cannot close:

  1. A command that PRINTS a JWT to stdout with NO backing env var at all
     (``gcloud auth print-identity-token``, ``kubectl create token``,
     ``az account get-access-token --query accessToken``). ``_redact_secrets``
     only masks the VALUES of credential-NAMED os.environ vars, so a bearer
     that never lived in an env var is never masked.

  2. A JWT carried by a BENIGN-named env var (``AUTH_HEADER``, ``IDENTITY``,
     ``API_RESPONSE``) -- the name dodges ``_matches_credential`` so the value
     is neither stripped from the child env nor added to the redaction set.

Both persist the bearer to ``~/.apm/logs/scripts.log`` in cleartext. A JWT is
a high-confidence, low-false-positive value shape (two ``eyJ`` base64url
segments joined by dots), so a structural masker is warranted.

Secret-looking values are assembled at runtime from a benign prefix + a short
fake token so nothing here resembles a real scannable secret.
"""

from __future__ import annotations

from apm_cli.core import script_executors as se


def _fake_jwt() -> str:
    # Structurally a JWT: eyJ<base64url-header>.eyJ<base64url-body>.<sig>.
    # Assembled at runtime; the segments are long enough (>= _MIN_REDACT_LEN)
    # to be eligible for masking but are pure fakes.
    header = "eyJ" + "hbGciOiJIUzI1NiJ9"
    body = "eyJ" + "zdWIiOiJyMjVmYWtlIn0"
    sig = "r25" + "FAKEsignature00"
    return f"{header}.{body}.{sig}"


def test_jwt_in_stdout_no_env_var_masked(monkeypatch, tmp_path):
    """A JWT printed to stdout by a command (no backing env var) must not
    persist to scripts.log -- structural shape is the only available signal."""
    secret = _fake_jwt()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    # Simulate: post-install runs `gcloud auth print-identity-token` which
    # emits a raw JWT to stdout. There is NO env var holding this value.
    se._append_to_script_log(
        "post-install",
        "command",
        "gcloud auth print-identity-token",
        stdout=secret,
    )
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert secret not in log, "JWT printed to stdout leaked to scripts.log in cleartext"


def test_jwt_under_benign_name_masked_in_log(monkeypatch, tmp_path):
    """A JWT carried by a benign-named var (AUTH_HEADER) the denylist misses
    must still be masked structurally when echoed to the log."""
    secret = _fake_jwt()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    # AUTH_HEADER is benign by NAME: AUTHORIZATION / AUTHTOKEN tokens do not
    # match it, so the value is never added to the redaction set.
    monkeypatch.setenv("AUTH_HEADER", "Bearer " + secret)
    se._append_to_script_log(
        "post-install",
        "command",
        "env",
        stdout=f"AUTH_HEADER=Bearer {secret}",
    )
    log = (tmp_path / "logs" / "scripts.log").read_text()
    assert secret not in log, "JWT under benign name leaked to scripts.log in cleartext"


def test_benign_name_holding_jwt_is_not_swept_from_env(monkeypatch):
    """Sanity: AUTH_HEADER is genuinely benign by NAME (so the leak is a VALUE
    gap, not a NAME gap) -- it is NOT stripped from the child env."""
    assert not se._matches_credential("AUTH_HEADER")
