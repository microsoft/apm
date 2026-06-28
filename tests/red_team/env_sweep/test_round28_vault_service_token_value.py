"""Round-28 env red-team: HashiCorp Vault service/batch token VALUE leak.

A lifecycle script that runs the Vault CLI (``vault token create``,
``vault login``, ``vault kv get -field=token``) prints a freshly minted
Vault token to stdout. Since Vault 1.10 these tokens carry a fixed
provider prefix:

    hvs.<base62...>   service token
    hvb.<base62...>   batch token
    hvr.<base62...>   recovery token

The token is NOT in ``os.environ`` (it was just minted by the CLI), so the
value-redaction scan in ``_redact_secrets`` (which only masks values of
denylisted *env vars*) never sees it. It has no ``@`` userinfo, no
``password=`` key, and no double-``eyJ`` JWT shape, so none of the
structural maskers (URL-cred, conn-string, webhook, PEM, JWT) fire either.

``_PROVIDER_TOKEN_PATTERN`` (the round-26/27 structural value masker) lists
gh*/github_pat_/xox*/Stripe/AIza/glpat-/xapp-/ya29./hf_/dop_v1_/tskey- but
has NO ``hv[sbr].`` arm -- so the cleartext Vault bearer persists in
``~/.apm/logs/scripts.log``.

Vault is one of the most widely deployed secret managers; emitting its
bearer token to a 0600 log in cleartext is a real credential-leak.

These tests assert the SECURE behaviour (token absent from redaction
output and from the on-disk log). They FAIL on this HEAD, proving the gap.
"""

from __future__ import annotations

import os
import secrets
import string
import threading

import pytest

from apm_cli.core import script_executors
from apm_cli.core.script_executors import _append_to_script_log, _redact_secrets


def _watchdog(seconds: float = 20.0):
    """Fail fast on a hang without using a shell timeout (banned)."""

    def _boom():
        os._exit(99)  # pragma: no cover - only fires on a genuine hang

    t = threading.Timer(seconds, _boom)
    t.daemon = True
    t.start()
    return t


def _fake_vault_token(prefix: str) -> str:
    """Build a Vault-shaped token at runtime (never a real secret literal)."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(64))
    return prefix + body


@pytest.mark.parametrize("prefix", ["hvs.", "hvb.", "hvr."])
def test_round28_vault_token_redacted_from_output(prefix):
    """A Vault token echoed to stdout must not survive _redact_secrets."""
    wd = _watchdog()
    try:
        token = _fake_vault_token(prefix)
        # Simulate `vault token create` stdout (no backing env var).
        stdout = f"key                value\n----               -----\ntoken              {token}\n"
        redacted = _redact_secrets(stdout)
        assert token not in redacted, (
            f"Vault {prefix} bearer token leaked through _redact_secrets: "
            f"cleartext value present in redaction output"
        )
    finally:
        wd.cancel()


def test_round28_vault_token_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real log writer must not persist a Vault token."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        # Ensure no backing env var holds the token -- it is freshly minted.
        for var in ("VAULT_TOKEN", "VAULT_DEV_ROOT_TOKEN_ID"):
            monkeypatch.delenv(var, raising=False)

        token = _fake_vault_token("hvs.")
        _append_to_script_log(
            "postinstall",
            "shell",
            "vault token create -format=table",
            stdout=f"token              {token}\ntoken_accessor     redacted-accessor\n",
            status="ok",
            exit_code=0,
        )

        log_path = script_executors._get_scripts_log_path()
        contents = log_path.read_text(encoding="utf-8")
        assert token not in contents, (
            "Vault service token persisted in cleartext in ~/.apm/logs/scripts.log; "
            f"found leaked bearer in log file at {log_path}"
        )
    finally:
        wd.cancel()
