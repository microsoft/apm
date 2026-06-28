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


class TestStructuralMaskers:
    """Name-independent structural maskers chained inside ``_redact_secrets``."""

    def test_connection_string_password_masked(self):
        out = se._redact_connection_string_password("host=db password=topSecretValue dbname=app")
        assert "topSecretValue" not in out
        assert "password=[REDACTED]" in out

    def test_odbc_pwd_in_dsn_masked(self):
        out = se._redact_connection_string_password("Driver=x;UID=sa;PWD=secretSlash123;")
        assert "secretSlash123" not in out

    def test_standalone_pwd_path_echo_preserved(self):
        text = "PWD=/home/user/project"
        assert se._redact_connection_string_password(text) == text

    def test_webhook_url_token_masked(self):
        host = "https://hooks.slack.com/services/"
        url = host + "T000/B000/" + "XyZsecretWebhookToken99"
        out = se._redact_webhook_urls("posting to " + url)
        assert "XyZsecretWebhookToken99" not in out

    def test_sas_signature_masked(self):
        out = se._redact_sas_signatures(
            "https://x.example/path?sv=2021&sig=HMACsecretValue123&se=z"
        )
        assert "HMACsecretValue123" not in out
        assert "sig=[REDACTED]" in out

    def test_pem_private_key_material_masked(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIsecretKeyBytes\n-----END RSA PRIVATE KEY-----"
        out = se._redact_pem_private_keys(pem)
        assert "MIIsecretKeyBytes" not in out
        assert "BEGIN RSA PRIVATE KEY" in out

    def test_bundler_source_credential_masked(self):
        out = se._redact_bundler_source_credentials(
            "BUNDLE_GEMS__CONTRIBSYS__COM=deploy:gemSourceSecret99"
        )
        assert "gemSourceSecret99" not in out
        assert "deploy:" in out

    def test_bundler_benign_config_untouched(self):
        for text in ("BUNDLE_PATH=vendor/bundle", "BUNDLE_JOBS=4"):
            assert se._redact_bundler_source_credentials(text) == text

    def test_embedded_url_credentials_masked(self):
        out = se._redact_embedded_url_credentials("clone https://bot:ghp_xZ9secret@github.com/o/r")
        assert "ghp_xZ9secret" not in out

    def test_bare_email_not_over_redacted(self):
        text = "contact user@example.com please"
        assert se._redact_embedded_url_credentials(text) == text

    def test_url_credentials_stripped(self):
        out = se._redact_url_credentials("https://bot:secretpw@example.com/path")
        assert "secretpw" not in out
        assert "example.com" in out


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


class TestEndToEndChain:
    """``_redact_secrets`` composes the value masker + all structural maskers."""

    def test_value_and_structural_both_applied(self, monkeypatch):
        monkeypatch.setenv("TF_TOKEN_app_terraform_io", "atlasv1.SeCretBearerToken123")
        text = (
            "TF_TOKEN_app_terraform_io=atlasv1.SeCretBearerToken123 "
            "BUNDLE_GEMS__CONTRIBSYS__COM=deploy:anotherGemSecret88"
        )
        out = se._redact_secrets(text)
        assert "SeCretBearerToken123" not in out
        assert "anotherGemSecret88" not in out


def teardown_module(_mod):
    """Ensure no test env var leaks into a later module's os.environ scan."""
    for name in ("APP_TOKEN", "SHORT_TOKEN", "LONG_TOKEN", "CRLF_TOKEN"):
        os.environ.pop(name, None)
