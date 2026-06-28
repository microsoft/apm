"""Round-7 env regression traps: multi-@ URL-cred leak + auth-blob name gap.

r7-env-1 (MED) -- the round-6 ``_EMBEDDED_URL_CRED_PATTERN`` used the userinfo
class ``[^/\\s@]+@`` which stops at the FIRST ``@``. A password may itself
contain a literal ``@`` (e.g. ``svc:p@ssw0rd@host``); git/curl treat the LAST
``@`` before the path as the separator, so the secret tail after the first
``@`` leaked to the 0600 scripts.log in cleartext. The fix widens the class to
``[^/\\s]+`` so the greedy match anchors to the last ``@`` before the path,
without crossing ``/`` or whitespace (so emails / queries stay un-redacted).

r7-env-2 (LOW) -- ``NPM_AUTH`` / ``REGISTRY_AUTH`` (real CI conventions:
``//registry.npmjs.org/:_authToken=${NPM_AUTH}``) were not in the curated
credential-blob name set, so their values echoed into scripts.log in cleartext.
The fix adds them to ``_CREDENTIAL_BLOB_NAMES`` (curated, not a blanket
``_AUTH$`` token, to avoid over-redacting ``AUTH_URL`` / ``OAUTH_*``).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se


@pytest.mark.parametrize(
    ("url", "leaked_fragment"),
    [
        ("https://svc:p@ssw0rd_S3cretTail@registry.example.com/o/r", "ssw0rd_S3cretTail"),
        ("https://u:a@b@c@host.example.com/path", "a@b@c"),
        ("ssh://git:pa@ss@github.com/o/r", "pa@ss"),
    ],
)
def test_multiat_password_fully_masked(tmp_path, monkeypatch, url, leaked_fragment):
    """A password containing literal '@' must be fully masked in the log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "http", url, status="ok")
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert leaked_fragment not in content, content
    assert "[REDACTED]@" in content, content
    # The real host survives so the audit entry is still useful.
    assert (
        "host.example.com" in content
        or "registry.example.com" in content
        or "github.com" in content
    )


def test_multiat_in_stdout_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log(
        "post-install",
        "command",
        "git clone",
        stdout="cloning https://bot:tok@en@SECRETTAIL_x@github.com/o/r done",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "SECRETTAIL_x" not in content, content
    assert "tok@en" not in content, content


@pytest.mark.parametrize(
    "keep",
    [
        "alice@example.com",
        "git@github.com:o/r",
        "https://github.com/o/r",
        "https://github.com/o/r?next=a@b",
        "scp user@host:/path/file",
    ],
)
def test_keep_cases_not_over_redacted(keep):
    """Emails, scp-form, queries and clean URLs must NOT be masked."""
    assert se._redact_embedded_url_credentials(keep) == keep


def test_npm_and_registry_auth_blobs_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("NPM_AUTH", "npmAuthSecretValue123")
    monkeypatch.setenv("REGISTRY_AUTH", "regAuthSecretValue456")
    se._append_to_script_log(
        "post-install",
        "command",
        "echo",
        stdout="token npmAuthSecretValue123 and regAuthSecretValue456",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "npmAuthSecretValue123" not in content, content
    assert "regAuthSecretValue456" not in content, content


def test_npm_auth_recognized_but_benign_auth_names_preserved():
    """Curated names match; AUTH_URL / OAUTH_* convention names stay benign."""
    assert se._matches_credential("NPM_AUTH")
    assert se._matches_credential("REGISTRY_AUTH")
    assert not se._matches_credential("AUTH_URL")
    assert not se._matches_credential("OAUTH_ENDPOINT")
