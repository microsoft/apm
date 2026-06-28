"""Round-17 env-exfiltration regression traps (PR #1798 lifecycle scripts).

Each test pins a structural log-redaction gap found by the round-17 adversarial
sweep against the round-16 fixes. All five leaked a realistic secret VALUE to the
0600 ``scripts.log`` under default settings:

  r17-env-1 (HIGH) OpenPGP ``PGP PRIVATE KEY BLOCK`` armor bypassed the PEM masker
                   (marker ends in ``KEY BLOCK`` not ``KEY``).
  r17-env-2 (MED)  Slack Workflow-Builder ``/triggers/`` webhook path bypassed the
                   host masker (only ``/services`` was keyed).
  r17-env-3 (MED)  Teams "Workflows" / Power Automate / Logic Apps webhooks carry
                   the secret as a ``?sig=`` SAS query token, not a path segment.
  r17-env-4 (MED)  libpq ``sslpassword=`` keyword bypassed ``\\b(password)`` (the
                   ``l`` before ``password`` is a word char -- no boundary).
  r17-env-5 (MED)  ODBC doubled-brace ``}}`` escape defeated the round-16 brace
                   value class (it stopped at the FIRST ``}`` and leaked the tail).

Webhook/PGP material is ASSEMBLED at runtime from fragments + obviously-fake
tokens so GitHub push-protection never sees a contiguous real-secret signature.
"""

from __future__ import annotations

import urllib.parse

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry

_FAKE = "FaKeReDtEaMtOkEn9988"


def _log_path(tmp_path):
    return tmp_path / "logs" / "scripts.log"


# --------------------------------------------------------------------------- #
# r17-env-1 -- OpenPGP "PGP PRIVATE KEY BLOCK" armor                            #
# --------------------------------------------------------------------------- #

_PGP_BODY = "lQVYBF" + ("k" * 140) + "ZmFrZQ=="


def _pgp_block():
    begin = "-----BEGIN PGP PRIVATE KEY BLOCK-----"
    end = "-----END PGP PRIVATE KEY BLOCK-----"
    return begin + "\n" + _PGP_BODY + "\n" + end


def test_r17_env_1_pgp_private_key_block_masked():
    """A ``gpg --export-secret-keys --armor`` dump must be masked by name-free armor."""
    out = se._redact_secrets("dump: " + _pgp_block())
    assert _PGP_BODY not in out, out
    assert "[REDACTED]" in out
    # Armor preserved so the log records a key was present.
    assert "PGP PRIVATE KEY BLOCK" in out


def test_r17_env_1_pgp_masked_in_log(tmp_path, monkeypatch):
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", "gpg-dump", stdout=_pgp_block(), status="ok"
    )
    content = _log_path(tmp_path).read_text()
    assert _PGP_BODY not in content, content
    assert "[REDACTED]" in content


@pytest.mark.parametrize(
    "label",
    [
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "PRIVATE KEY",
    ],
)
def test_r17_env_1_classic_pem_armor_still_masked(label):
    """The widened armor must NOT regress the classic ``... PRIVATE KEY`` forms."""
    body = "AAAA" + ("B" * 120)
    pem = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
    out = se._redact_secrets(pem)
    assert body not in out, out
    assert "[REDACTED]" in out


# --------------------------------------------------------------------------- #
# r17-env-2 -- Slack Workflow-Builder /triggers/ webhook                        #
# --------------------------------------------------------------------------- #


def test_r17_env_2_slack_triggers_path_masked():
    url = "https://hooks.slack.com/triggers/T01/3000/" + _FAKE
    out = se._redact_secrets("curl -X POST " + url)
    assert _FAKE not in out, out
    assert "[REDACTED]" in out
    masked = next(tok for tok in out.split() if tok.startswith("https://"))
    assert urllib.parse.urlparse(masked).hostname == "hooks.slack.com"


def test_r17_env_2_slack_services_still_masked():
    """The added /triggers alternative must NOT regress the /services form."""
    url = "https://hooks.slack.com/services/T0/B1/" + _FAKE
    out = se._redact_secrets("post " + url)
    assert _FAKE not in out, out
    assert "[REDACTED]" in out


# --------------------------------------------------------------------------- #
# r17-env-3 -- Azure / Teams-Workflows ?sig= SAS token                          #
# --------------------------------------------------------------------------- #

_SIG = "S1gN" + ("9" * 42)


@pytest.mark.parametrize(
    "url",
    [
        "https://contoso.environment.api.powerplatform.com/powerautomate/automations/"
        "direct/workflows/x/triggers/manual/paths/invoke?sig=" + _SIG,
        "https://prod-7.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/"
        "invoke?api-version=2016-06-01&sp=%2Ftriggers%2Fmanual%2Frun&sig=" + _SIG,
        "https://myacct.blob.core.windows.net/c/b.txt?sv=2022-11-02&se=x&sig=" + _SIG,
    ],
)
def test_r17_env_3_sas_signature_masked(url):
    out = se._redact_secrets("posting " + url)
    assert _SIG not in out, out
    assert "[REDACTED]" in out
    # The sig= key stays so the log shows a SAS was present.
    assert "sig=" in out


def test_r17_env_3_sas_masked_in_log(tmp_path, monkeypatch):
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    url = "https://x.logic.azure.com/workflows/a/triggers/manual/paths/invoke?sig=" + _SIG
    se._append_to_script_log("post-install", "command", "notify-teams", stdout=url, status="ok")
    content = _log_path(tmp_path).read_text()
    assert _SIG not in content, content
    assert "[REDACTED]" in content


def test_r17_env_3_sig_only_masks_the_value():
    """A trailing query param after sig= must keep its own key/value boundary."""
    out = se._redact_secrets("?sig=" + _SIG + "&foo=bar")
    assert _SIG not in out
    assert "foo=bar" in out, out


# --------------------------------------------------------------------------- #
# r17-env-4 -- libpq sslpassword= keyword                                       #
# --------------------------------------------------------------------------- #


def test_r17_env_4_libpq_sslpassword_masked():
    dsn = "host=db port=5432 dbname=app user=svc sslpassword=Sup3rSecretPhrase99"
    out = se._redact_secrets(dsn)
    assert "Sup3rSecretPhrase99" not in out, out
    assert "[REDACTED]" in out


def test_r17_env_4_plain_password_still_masked():
    out = se._redact_secrets("password=Sup3rSecretPhrase99")
    assert "Sup3rSecretPhrase99" not in out, out


def test_r17_env_4_passwd_still_masked():
    out = se._redact_secrets("passwd=Sup3rSecretPhrase99")
    assert "Sup3rSecretPhrase99" not in out, out


# --------------------------------------------------------------------------- #
# r17-env-5 -- ODBC doubled-brace }} escape                                     #
# --------------------------------------------------------------------------- #


def test_r17_env_5_odbc_doubled_brace_pwd_fully_masked():
    dsn = "Driver={ODBC Driver 18};Server=db;UID=sa;PWD={ab}}cd}"
    out = se._redact_secrets(dsn)
    # The whole brace-quoted value (incl. the escaped }) must be gone.
    assert out == "Driver={ODBC Driver 18};Server=db;UID=sa;PWD=[REDACTED]", repr(out)


def test_r17_env_5_odbc_doubled_brace_password_fully_masked():
    dsn = "Server=db;password={p}}w;d};Trusted=no"
    out = se._redact_secrets(dsn)
    assert "}}w;d}" not in out, out
    assert "[REDACTED]" in out


def test_r17_env_5_simple_brace_still_masked():
    """The round-16 single-brace case must keep working."""
    out = se._redact_secrets("UID=sa;PWD={p;w@d}")
    assert out == "UID=sa;PWD=[REDACTED]", repr(out)


# --------------------------------------------------------------------------- #
# regressions -- the benign $PWD echo and webhook child-env survival            #
# --------------------------------------------------------------------------- #


def test_r17_pwd_path_echo_preserved():
    assert se._redact_secrets("PWD=/home/user") == "PWD=/home/user"
    assert se._redact_secrets("OLDPWD=/var/tmp") == "OLDPWD=/var/tmp"


def test_r17_dsn_pwd_slash_value_still_masked():
    assert se._redact_secrets("UID=sa;PWD=/Sl4shStartP4ss;") == "UID=sa;PWD=[REDACTED];"


def test_r17_webhook_url_survives_in_child_env(monkeypatch):
    """Masking is LOG-ONLY: the trigger URL must still reach the child env."""
    url = "https://hooks.slack.com/triggers/T0/1/" + _FAKE
    monkeypatch.setenv("SLACK_WORKFLOW_URL", url)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert env.get("SLACK_WORKFLOW_URL") == url
