"""Unit coverage for the script_executors log-redaction / credential helpers.

These pure helpers gate what reaches ``~/.apm/logs/scripts.log`` and the child
environment. They were previously exercised only by the adversarial red-team
suite (which CI does not collect), so this module pins their behaviour inside
``tests/unit`` where the coverage gate measures it.
"""

import os

from apm_cli.core import script_executors as se


class TestMatchesCredential:
    """``_matches_credential`` -- which env-var NAMES hold a secret."""

    def test_suffix_token_names_match(self):
        for name in ("API_TOKEN", "DB_PASSWORD", "MY_SECRET", "SERVICE_PAT", "GPG_PASSPHRASE"):
            assert se._matches_credential(name) is True, name

    def test_working_directory_vars_exempt(self):
        assert se._matches_credential("PWD") is False
        assert se._matches_credential("OLDPWD") is False

    def test_terraform_cloud_prefix_matches(self):
        """``TF_TOKEN_<host>`` is a Terraform Cloud bearer (token is an infix)."""
        assert se._matches_credential("TF_TOKEN_app_terraform_io") is True
        assert se._matches_credential("TF_TOKEN_my_corp_example_com") is True

    def test_benign_terraform_vars_preserved(self):
        for name in ("TF_VAR_region", "TF_LOG", "TF_CLI_ARGS"):
            assert se._matches_credential(name) is False, name

    def test_curated_blob_names_match(self):
        for name in ("DOCKER_AUTH_CONFIG", "SECRET_KEY_BASE", "WALLET_SEED"):
            assert se._matches_credential(name) is True, name

    def test_rng_seeds_not_stripped(self):
        for name in ("PYTHONHASHSEED", "RANDOM_SEED"):
            assert se._matches_credential(name) is False, name

    def test_blob_suffix_match(self):
        assert se._matches_credential("PRIMARY_DSN") is True
        assert se._matches_credential("ARTIFACTORY_AUTH") is True

    def test_bundler_source_var_not_denylisted(self):
        """Bundler source creds must stay in the child env (masked only in logs)."""
        assert se._matches_credential("BUNDLE_GEMS__CONTRIBSYS__COM") is False


class TestRedactSecretsValueMasking:
    """``_redact_secrets`` -- mask known env-var VALUES echoed by a script."""

    def test_masks_long_credential_value(self, monkeypatch):
        monkeypatch.setenv("APP_TOKEN", "abcd1234efgh5678ijkl")
        out = se._redact_secrets("echo abcd1234efgh5678ijkl here")
        assert "abcd1234efgh5678ijkl" not in out
        assert "[REDACTED]" in out

    def test_short_value_not_masked(self, monkeypatch):
        monkeypatch.setenv("APP_TOKEN", "abc")
        assert se._redact_secrets("value abc shown") == "value abc shown"

    def test_longest_first_no_fragment_leak(self, monkeypatch):
        monkeypatch.setenv("SHORT_TOKEN", "abcd1234")
        monkeypatch.setenv("LONG_TOKEN", "abcd1234efgh5678")
        out = se._redact_secrets("blob abcd1234efgh5678 end")
        assert "efgh5678" not in out

    def test_carriage_return_normalized_value_masked(self, monkeypatch):
        monkeypatch.setenv("CRLF_TOKEN", "line1value\r\nline2value")
        out = se._redact_secrets("got line1value\nline2value done")
        assert "line2value" not in out

    def test_empty_text_returns_empty(self):
        assert se._redact_secrets("") == ""


class TestNeutralizeNewlines:
    """``_neutralize_newlines`` -- prevent forged column-0 audit records."""

    def test_crlf_escaped(self):
        out = se._neutralize_newlines("real\r\nforged")
        assert "\n" not in out
        assert "\\r" in out or "\\n" in out

    def test_unicode_line_separators_escaped(self):
        out = se._neutralize_newlines("a\u2028b\u2029c")
        assert "\u2028" not in out
        assert "\u2029" not in out

    def test_plain_text_unchanged(self):
        assert se._neutralize_newlines("plain text here") == "plain text here"


class TestRedactSecretsScopeBoundary:
    """``_redact_secrets`` masks known-named env-var VALUES only.

    Shared-responsibility: APM redacts the values of credentials it itself
    manages (denylisted env-var names), but it does NOT shape-scan a script's
    own third-party secrets out of stdout/stderr -- that is the script
    author's responsibility, not the package manager's.
    """

    def test_known_named_value_masked(self, monkeypatch):
        monkeypatch.setenv("TF_TOKEN_app_terraform_io", "atlasv1.SeCretBearerToken123")
        out = se._redact_secrets("TF_TOKEN_app_terraform_io=atlasv1.SeCretBearerToken123")
        assert "SeCretBearerToken123" not in out

    def test_unbacked_third_party_secret_not_shape_scanned(self):
        text = "BUNDLE_GEMS__CONTRIBSYS__COM=deploy:anotherGemSecret88"
        assert se._redact_secrets(text) == text


def teardown_module(_mod):
    """Ensure no test env var leaks into a later module's os.environ scan."""
    for name in ("APP_TOKEN", "SHORT_TOKEN", "LONG_TOKEN", "CRLF_TOKEN"):
        os.environ.pop(name, None)
