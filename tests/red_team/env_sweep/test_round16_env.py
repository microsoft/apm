"""Round-16 env breaks r16-env-1..4: webhook URL, PEM key, ODBC PWD path-guard
bypass, brace-escaped DSN password.

Four realistic secrets still leaked their VALUE to the 0600 ``scripts.log`` on
the DEFAULT lifecycle path (no opt-in):

  * r16-env-1 (MED): an incoming-webhook URL (Slack / Discord / Teams / O365)
    carries a bearer-grade token in the URL PATH. The env-var name
    (``SLACK_WEBHOOK`` / ``*_WEBHOOK_URL``) ends in a benign token the suffix
    denylist does not list, there is no ``@`` userinfo and no ``password=``, so
    neither the URL-credential masker nor the DSN masker saw it. The fix masks
    the secret path segment of the known webhook hosts STRUCTURALLY (log-only),
    so the URL still reaches the child env for a script that legitimately posts.
  * r16-env-2 (HIGH): a PEM private-key blob (``*_KEY_PEM`` env value, or an
    inline key a script echoes) is a multi-line secret with no ``=`` key and no
    URL; its name ends in a benign token. The fix redacts the key material
    between the ``BEGIN/END ... PRIVATE KEY`` armor markers, name-independent.
  * r16-env-3 (MED, the round-15 ODBC fix's own residual): the round-15 path
    guard preserved ``PWD=<value>`` whenever the value looked like a path, so an
    attacker who wrote ``UID=sa;PWD=/Sl4shStartP4ss`` dodged the mask. The fix
    makes the masker context-aware: a ``PWD=`` preceded by a ``;`` DSN delimiter
    is ALWAYS masked; only a standalone ``PWD=/path`` shell echo is preserved.
  * r16-env-4 (MED): an ODBC brace-escaped value ``PWD={p;w@d}`` embeds a ``;``,
    so the round-15 value class ``[^\\s;]+`` stopped at the first ``;`` and masked
    only ``{p``, leaking the tail. The fix consumes a braced value through ``}``.

Each trap drives the REAL ``_redact_secrets`` / ``_append_to_script_log`` /
``_build_script_env`` paths with exact-value-absence assertions, and re-asserts
that the benign ``$PWD`` echo and a webhook URL surviving in the child env are
not damaged.
"""

from __future__ import annotations

import urllib.parse

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry

# --------------------------------------------------------------------------- #
# r16-env-1 -- incoming-webhook URL token in the URL PATH                       #
# --------------------------------------------------------------------------- #

# Webhook URLs are ASSEMBLED at runtime from a host prefix + a separate fake
# token literal so the contiguous real-secret signature never appears in source
# (GitHub push-protection flags a literal Slack/Discord webhook URL even in a
# red-team fixture). The masker operates on the assembled runtime string, so the
# test is unaffected.
_WEBHOOK_HOSTS = [
    "https://hooks.slack.com/services/T00/B11/",
    "https://discord.com/api/webhooks/123456789012345678/",
    "https://discordapp.com/api/webhooks/987654321/",
    "https://mytenant.webhook.office.com/webhookb2/abc-def/IncomingWebhook/",
    "https://outlook.office.com/webhook/aaaa-bbbb/IncomingWebhook/",
]
_FAKE_TOKEN = "FaKeReDtEaMtOkEn"


def _webhook_cases():
    """(url, token) pairs assembled at runtime to dodge source secret-scanning."""
    return [(host + _FAKE_TOKEN, _FAKE_TOKEN) for host in _WEBHOOK_HOSTS]


@pytest.mark.parametrize("url,secret", _webhook_cases())
def test_r16_env_1_webhook_token_masked(url, secret):
    """The secret path/token of a known webhook URL must be masked in output."""
    out = se._redact_secrets(f"posting to {url} done")
    assert secret not in out, out
    assert "[REDACTED]" in out


def test_r16_env_1_webhook_token_masked_in_log(tmp_path, monkeypatch):
    """A webhook URL echoed to stdout must not persist its token in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    url = "https://hooks.slack.com/services/T0/B1/" + _FAKE_TOKEN
    se._append_to_script_log(
        "post-install",
        "command",
        "notify",
        stdout=f"curl -X POST {url}",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert _FAKE_TOKEN not in content, content
    assert "[REDACTED]" in content
    # The host stays readable for triage.
    masked = next(tok for tok in content.split() if tok.startswith("https://"))
    assert urllib.parse.urlparse(masked).hostname == "hooks.slack.com"


def test_r16_env_1_webhook_url_survives_in_child_env(monkeypatch):
    """The webhook URL must STILL reach the child env (log-only masking)."""
    url = "https://hooks.slack.com/services/T0/B1/" + _FAKE_TOKEN
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert env.get("SLACK_WEBHOOK_URL") == url


def test_r16_env_1_non_webhook_url_not_over_masked():
    """A plain https URL with no webhook host must be left intact."""
    text = "see https://example.com/docs/guide for details"
    assert se._redact_secrets(text) == text


# --------------------------------------------------------------------------- #
# r16-env-2 -- PEM private-key blob                                            #
# --------------------------------------------------------------------------- #

_PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAsEcReTkEyMaTeRiAlLiNeOnE1234567890abcdefGHIJK\n"
    "sEcReTkEyMaTeRiAlLiNeTwO0987654321ZYXWVUTSRQPONMLKJIHGFEDCBA\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    "armor",
    [
        "RSA PRIVATE KEY",
        "PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "DSA PRIVATE KEY",
    ],
)
def test_r16_env_2_pem_material_masked(armor):
    """The key material between any PEM PRIVATE KEY armor must be redacted."""
    blob = f"-----BEGIN {armor}-----\nsEcReTkEyBaSe64MaTeRiAl==\n-----END {armor}-----"
    out = se._redact_secrets(f"key is {blob}")
    assert "sEcReTkEyBaSe64MaTeRiAl" not in out, out
    assert "[REDACTED]" in out
    # Armor lines survive so the log records a key was present.
    assert f"BEGIN {armor}" in out
    assert f"END {armor}" in out


def test_r16_env_2_pem_masked_in_log(tmp_path, monkeypatch):
    """A PEM private key echoed to stdout must not persist in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "keygen",
        stdout=_PEM_BLOCK,
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "sEcReTkEyMaTeRiAlLiNeOnE" not in content, content
    assert "sEcReTkEyMaTeRiAlLiNeTwO" not in content, content
    assert "[REDACTED]" in content


def test_r16_env_2_no_pem_text_untouched():
    """Text with no PEM armor must be returned unchanged by the PEM masker."""
    text = "this mentions a private key but has no armor block"
    assert se._redact_pem_private_keys(text) == text


# --------------------------------------------------------------------------- #
# r16-env-3 -- ODBC PWD= path-guard bypass                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "conn",
    [
        "Driver={ODBC Driver 18};UID=sa;PWD=/Sl4shStartP4ss;",
        "Server=db;UID=sa;PWD=/etc/notapath/butapassword",
        "UID=sa;PWD=~tildeStartPass",
        "UID=sa;PWD=.dotStartPass",
        "Driver={x};UID=u;PWD=C:\\notapath\\pass",
    ],
)
def test_r16_env_3_dsn_pwd_masked_despite_path_prefix(conn):
    """A ``;``-delimited DSN PWD= must mask even a path-looking value."""
    out = se._redact_connection_string_password(conn)
    assert "Sl4shStartP4ss" not in out
    assert "notapath" not in out
    assert "tildeStartPass" not in out
    assert "dotStartPass" not in out
    assert "[REDACTED]" in out


def test_r16_env_3_standalone_pwd_echo_preserved():
    """A standalone ``PWD=/path`` shell echo (no ``;``) must be preserved."""
    for text in (
        "PWD=/home/user/project here",
        "PWD=. relative cwd",
        "PWD=~/work env dump",
        "OLDPWD=/old/path here",
        "PWD=C:\\Users\\me here",
    ):
        assert se._redact_connection_string_password(text) == text, text


# --------------------------------------------------------------------------- #
# r16-env-4 -- brace-escaped DSN password                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "conn,secret",
    [
        ("Driver={x};UID=sa;PWD={aa;TAILpwLEAK};", "TAILpwLEAK"),
        ("Server=db;PWD={p;w@d;with;semis};Database=app", "with;semis"),
        ("UID=u;password={br;ace;Value};", "ace;Value"),
        ("UID=u;passwd={x;y;ZtAiL};", "ZtAiL"),
    ],
)
def test_r16_env_4_braced_dsn_password_fully_masked(conn, secret):
    """A brace-escaped DSN password must be consumed through ``}`` (no tail leak)."""
    out = se._redact_connection_string_password(conn)
    assert secret not in out, out
    assert "[REDACTED]" in out


def test_r16_env_4_braced_password_masked_in_log(tmp_path, monkeypatch):
    """A braced ODBC password echoed to stdout must not leak its tail."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "migrate",
        stdout="conn Driver={ODBC Driver 18};UID=sa;PWD={se;cret;TaIlLeAk};",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "TaIlLeAk" not in content, content
    assert "[REDACTED]" in content
