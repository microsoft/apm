"""Round-26 red-team probes: structural PROVIDER-TOKEN value leaks in scripts.log.

The round-25 fold added a JWT VALUE masker (`_redact_jwt_values`, the double-`eyJ`
structural anchor) precisely because a bearer printed to stdout with NO backing
credential-named env var is matched by no NAME and no other structural masker, so
it persists in cleartext in ~/.apm/logs/scripts.log. The module comment is
explicit: "a JWT printed to stdout with NO backing env var ... is matched by no
NAME and no other structural masker, so it would persist in cleartext".

That exact reasoning is NOT JWT-specific. The same class of commonly-printed,
high-confidence, fixed-PREFIX provider credentials shares the gap:

  * GitHub PAT  -- ``gh auth token`` / ``git credential fill`` emit
    ``ghp_`` / ``gho_`` / ``ghs_`` / ``ghu_`` / ``ghr_`` / ``github_pat_`` tokens
    to stdout. APM is a GitHub-native tool, so a lifecycle script surfacing the
    host token is the single most likely real-world leak.
  * Slack bot/user token -- ``xoxb-`` / ``xoxp-`` / ``xoxa-`` / ``xoxr-``.
  * Stripe live secret key -- ``sk_live_`` / ``rk_live_``.
  * Google API key -- ``AIza`` (39 chars).

Each is a FULL secret (not a mere identifier) with a documented, unambiguous
prefix and a fixed-shape body, so a structural value masker is warranted and
carries near-zero false-positive risk -- exactly the bar `_redact_jwt_values`
already met for JWTs.

None of these is a JWT (no ``eyJ...eyJ`` shape), none carries URL userinfo / an
``@`` host (so `_redact_embedded_url_credentials` misses), none is a webhook URL,
PEM block, connection string, SAS sig, or Bundler pair. When printed to stdout
with no backing env var they are matched by NOTHING and reach scripts.log raw.

A genuine break here = the secure assertion (secret ABSENT from scripts.log)
FAILS against current code.

Secret-looking values are assembled at RUNTIME from a benign prefix + a short
fake body so nothing here resembles a real scannable secret (push-protection).
"""

from __future__ import annotations

from apm_cli.core import script_executors as se


def _fake_github_pat() -> str:
    # ghp_ + 36 base62 chars (GitHub fine-grained/classic PAT shape).
    return "ghp_" + "r26FAKEr26FAKEr26FAKEr26FAKEr26FAKE0"


def _fake_github_pat_new() -> str:
    # github_pat_ prefixed fine-grained token shape.
    return "github_pat_" + "11AAAA000_" + "r26fakeFINEgrainedTOKENbodyxxxxxxxxxx"


def _fake_slack_bot_token() -> str:
    return "xoxb-" + "111111111111-2222222222222-" + "r26FAKEslackTOKENbody00"


def _fake_stripe_live_key() -> str:
    return "sk_live_" + "r26FAKEstripeLIVEsecret000"


def _fake_google_api_key() -> str:
    return "AIza" + "Sy" + "r26FAKEgoogleAPIkeyBODYxxxxxxxxxxx"


def _read_log(tmp_path) -> str:
    return (tmp_path / "logs" / "scripts.log").read_text()


# --- Primary (HIGH): GitHub PAT, the APM-native leak ------------------------


def test_github_pat_in_stdout_no_env_var_masked(monkeypatch, tmp_path):
    """A GitHub PAT printed to stdout by ``gh auth token`` (no backing env var)
    must not persist to scripts.log -- structural shape is the only signal."""
    secret = _fake_github_pat()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    # Defensive: ensure no credential-named env var backs the value, so the
    # only thing that could mask it is a structural masker.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    se._append_to_script_log(
        "post-install",
        "command",
        "gh auth token",
        stdout=secret,
    )
    log = _read_log(tmp_path)
    assert secret not in log, "GitHub PAT printed to stdout leaked to scripts.log in cleartext"


def test_github_pat_under_benign_name_masked_in_log(monkeypatch, tmp_path):
    """A GitHub PAT carried by a benign-named var (HOST_TOKEN dodges nothing,
    but RESPONSE / IDENTITY do) echoed to the log must still be masked."""
    secret = _fake_github_pat()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    # Benign name -- not matched by _matches_credential -> value not in redaction
    # set and not stripped from child env.
    monkeypatch.setenv("CI_AUTH_RESPONSE", secret)
    se._append_to_script_log(
        "pre-install",
        "command",
        "echo $CI_AUTH_RESPONSE",
        stdout=secret,
    )
    log = _read_log(tmp_path)
    assert secret not in log, "GitHub PAT under benign env name leaked to scripts.log"


def test_github_pat_new_format_in_stderr_masked(monkeypatch, tmp_path):
    """The newer ``github_pat_`` fine-grained token shape printed BARE to
    stderr (a verbose git/diagnostic dump that is NOT a ``password=`` /
    connection-string assignment) must not persist to the log. The bare shape
    is the genuine gap -- a ``password=<github_pat_...>`` form is already caught
    by the connection-string masker, so we assert the unguarded shape here."""
    secret = _fake_github_pat_new()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "git -c credential.helper= ls-remote",
        stderr="using token " + secret + " for github.com",
    )
    log = _read_log(tmp_path)
    assert secret not in log, "github_pat_ token leaked to scripts.log via stderr"


# --- Same class (MED): other high-confidence prefixed provider secrets ------


def test_slack_bot_token_in_stdout_masked(monkeypatch, tmp_path):
    """A Slack bot token (xoxb-) printed to stdout must not persist raw."""
    secret = _fake_slack_bot_token()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "cat .slack-token",
        stdout=secret,
    )
    log = _read_log(tmp_path)
    assert secret not in log, "Slack bot token leaked to scripts.log in cleartext"


def test_stripe_live_secret_key_in_stdout_masked(monkeypatch, tmp_path):
    """A Stripe live secret key (sk_live_) printed to stdout must not persist."""
    secret = _fake_stripe_live_key()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "vault kv get -field=key secret/stripe",
        stdout=secret,
    )
    log = _read_log(tmp_path)
    assert secret not in log, "Stripe live secret key leaked to scripts.log in cleartext"


def test_google_api_key_in_stdout_masked(monkeypatch, tmp_path):
    """A Google API key (AIza...) printed to stdout must not persist raw."""
    secret = _fake_google_api_key()
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "gcloud services api-keys ...",
        stdout=secret,
    )
    log = _read_log(tmp_path)
    assert secret not in log, "Google API key leaked to scripts.log in cleartext"


# --- Regression evidence: shapes the code DOES correctly defend -------------


def test_jwt_still_masked_regression(monkeypatch, tmp_path):
    """Round-25 JWT value masker still fires (control: confirms harness wired
    to the real append path and a known-closed vector stays closed)."""
    header = "eyJ" + "hbGciOiJIUzI1NiJ9"
    body = "eyJ" + "zdWIiOiJyMjZjdHJsIn0"
    jwt = f"{header}.{body}.r26ctrlSIGfake00"
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "kubectl create token", stdout=jwt)
    log = _read_log(tmp_path)
    assert jwt not in log, "round-25 JWT regression: bearer leaked"


def test_benign_ghp_like_word_not_overredacted(monkeypatch, tmp_path):
    """False-positive guard: a benign token that merely starts with the
    letters g-h-p but is NOT a ``ghp_<36 base62>`` PAT must survive intact,
    so a structural fix stays surgical."""
    benign = "ghpost-graphql-helper"  # not ghp_ + 36 chars
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "echo build", stdout=benign)
    log = _read_log(tmp_path)
    assert benign in log, "benign ghp-like word was over-redacted"
