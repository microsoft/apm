"""Round-27 red-team probe (ENV / SECRET-EXFIL): provider-token VALUE shapes the
structural masker still misses.

Round-25 added a JWT value masker and round-26 added ``_redact_provider_tokens``
(``_PROVIDER_TOKEN_PATTERN``) precisely because a provider secret printed to
stdout with NO backing credential-named env var is matched by no NAME and no
other structural masker, so it persists in cleartext in ~/.apm/logs/scripts.log.
The module comment for ``_redact_provider_tokens`` is explicit: it "catches a raw
provider secret printed to stdout/stderr with no backing env var and no
surrounding delimiter."

  r27-env-2 (provider-prefix coverage gap): ``_PROVIDER_TOKEN_PATTERN`` is a
    CLOSED allowlist of exactly five prefixes -- ``gh[posur]_`` / ``github_pat_``
    / ``xox[baprs]-`` / ``[sr]k_live_`` / ``AIza``. Every OTHER high-confidence,
    documented, fixed-prefix provider secret in the same class falls through and
    reaches scripts.log raw when echoed by ``vault kv get`` / ``cat <file>`` /
    a verbose CLI dump:

      * GitLab personal access token   -- ``glpat-`` (full bearer).
      * Slack APP-level token          -- ``xapp-`` (the ``xox[baprs]`` arm only
                                          covers bot/user/app-config/refresh, NOT
                                          the ``xapp-`` socket-mode app token).
      * Google OAuth access token      -- ``ya29.`` (a full bearer; the ``AIza``
                                          arm only covers static API keys).
      * Hugging Face token             -- ``hf_``.
      * DigitalOcean PAT               -- ``dop_v1_``.
      * Tailscale auth key             -- ``tskey-``.

    Each is a FULL secret (not a mere public identifier) with a documented,
    unambiguous prefix and a fixed-shape body, so a structural value masker is
    warranted and carries near-zero false-positive risk -- exactly the bar
    ``_redact_jwt_values`` / ``_redact_provider_tokens`` already met.

    None is a JWT (no ``eyJ.eyJ``), none carries URL userinfo / an ``@`` host,
    none is a webhook URL, PEM block, connection string, SAS sig, or Bundler pair.
    Printed to stdout with no backing env var they are matched by NOTHING and
    reach scripts.log in cleartext.

  Fix: extend ``_PROVIDER_TOKEN_PATTERN`` with the additional fixed-prefix arms
  (``glpat-[A-Za-z0-9_-]{20,}``, ``xapp-[A-Za-z0-9-]{10,}``,
  ``ya29\\.[A-Za-z0-9_-]{20,}``, ``hf_[A-Za-z0-9]{30,}``,
  ``dop_v1_[A-Za-z0-9]{40,}``, ``tskey-[A-Za-z0-9-]{10,}``).

A genuine break = the SECURE assertion (secret ABSENT from scripts.log) FAILS
against current code.

Secret-looking values are assembled at RUNTIME from a benign prefix + a short
fake body so nothing here resembles a real scannable secret (push-protection).
"""

from __future__ import annotations

from apm_cli.core import script_executors as se


def _read_log(tmp_path) -> str:
    return (tmp_path / "logs" / "scripts.log").read_text()


def _fake_gitlab_pat() -> str:
    return "glpat-" + "r27FAKEgitlabPATbodyABCD"


def _fake_slack_app_token() -> str:
    return "xapp-" + "1-A00000000-1111111111111-" + "r27FAKEslackAPPbodyXY"


def _fake_gcp_access_token() -> str:
    return "ya29." + "a0AfB_r27FAKEgcpACCESStokenBODY" + "x" * 24


def _fake_hf_token() -> str:
    return "hf_" + "r27FAKEhuggingfaceTOKENbodyABCDEFGH"


def _fake_digitalocean_pat() -> str:
    return "dop_v1_" + "r27fakeDOtokenbody" + "a" * 50


def _fake_tailscale_key() -> str:
    return "tskey-auth-" + "r27FAKEtailscaleKEYbodyABCDEFGHJK"


# --- Each shape printed to stdout with NO backing env var -------------------


def test_gitlab_pat_in_stdout_masked(monkeypatch, tmp_path):
    secret = _fake_gitlab_pat()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "glab auth status", stdout=secret)
    assert secret not in _read_log(tmp_path), "GitLab glpat- token leaked to scripts.log"


def test_slack_app_token_in_stdout_masked(monkeypatch, tmp_path):
    secret = _fake_slack_app_token()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "cat .slack-app-token", stdout=secret)
    assert secret not in _read_log(tmp_path), "Slack xapp- app token leaked to scripts.log"


def test_gcp_access_token_in_stdout_masked(monkeypatch, tmp_path):
    secret = _fake_gcp_access_token()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", "gcloud auth print-access-token", stdout=secret
    )
    assert secret not in _read_log(tmp_path), "GCP ya29. access token leaked to scripts.log"


def test_huggingface_token_in_stdout_masked(monkeypatch, tmp_path):
    secret = _fake_hf_token()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "cat ~/.huggingface/token", stdout=secret)
    assert secret not in _read_log(tmp_path), "Hugging Face hf_ token leaked to scripts.log"


def test_digitalocean_pat_in_stderr_masked(monkeypatch, tmp_path):
    secret = _fake_digitalocean_pat()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", "doctl auth init", stderr="using token " + secret
    )
    assert secret not in _read_log(tmp_path), "DigitalOcean dop_v1_ token leaked to scripts.log"


def test_tailscale_key_in_stdout_masked(monkeypatch, tmp_path):
    secret = _fake_tailscale_key()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install", "command", "vault kv get -field=key secret/ts", stdout=secret
    )
    assert secret not in _read_log(tmp_path), "Tailscale tskey- auth key leaked to scripts.log"


# --- Controls: round-26 shapes still masked; benign lookalikes survive ------


def test_round26_ghp_regression(monkeypatch, tmp_path):
    """Control: the round-26 ``ghp_`` masker still fires (harness wired to the
    real append path; a known-closed vector stays closed)."""
    secret = "ghp_" + "r26FAKEr26FAKEr26FAKEr26FAKEr26FAKE0"
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "gh auth token", stdout=secret)
    assert secret not in _read_log(tmp_path), "round-26 ghp_ regression: PAT leaked"


def test_benign_glpat_like_word_not_overredacted(monkeypatch, tmp_path):
    """False-positive guard: a benign word that merely starts with the same
    letters but is NOT a ``glpat-<20+>`` token must survive intact."""
    benign = "glpatrol-build-helper"  # not glpat- + 20 chars
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "echo build", stdout=benign)
    assert benign in _read_log(tmp_path), "benign glpat-like word was over-redacted"
