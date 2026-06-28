"""Round-8 env regression trap: ``*_PASSPHRASE`` key-passphrase leak.

r8-env-1 (MED) -- the credential denylist alternation carried the
``PASSWORD`` / ``PASSWD`` / ``PWD`` family but NOT ``PASSPHRASE``, so a
GPG / SSH key passphrase env var (``GPG_PASSPHRASE``,
``SSH_KEY_PASSPHRASE``) was never recognised as a credential: its value
echoed into the 0600 scripts.log in cleartext, and it stayed expandable
into HTTP headers / command env with no credential-block warning -- even
though its ``PASSWORD``-named sibling under the identical echo IS masked.
The fix adds ``PASSPHRASE`` to ``_CREDENTIAL_DENYLIST``.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se


@pytest.mark.parametrize(
    "name",
    ["GPG_PASSPHRASE", "SSH_KEY_PASSPHRASE", "ANSIBLE_VAULT_PASSPHRASE"],
)
def test_passphrase_names_recognised(name):
    """A ``*_PASSPHRASE`` name is the same secret class as ``*_PASSWORD``."""
    assert se._matches_credential(name)


@pytest.mark.parametrize(
    "benign",
    ["PASSAGE", "SURPASS", "PWD", "OLDPWD", "PASSENGER_COUNT"],
)
def test_benign_names_not_over_matched(benign):
    """Words that merely contain the substring must NOT be denylisted."""
    assert not se._matches_credential(benign)


def test_passphrase_value_redacted_in_log(tmp_path, monkeypatch):
    """A passphrase echoed to stdout must be masked, like its PASSWORD sibling."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("GPG_PASSPHRASE", "TopSecretPassphrase123")
    monkeypatch.setenv("GPG_PASSWORD", "TopSecretPasswordABC456")
    se._append_to_script_log(
        "post-install",
        "command",
        "gpg --decrypt secrets.gpg",
        stdout="debug: using TopSecretPassphrase123 and TopSecretPasswordABC456",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    # The passphrase must be gone -- exactly like the PASSWORD control.
    assert "TopSecretPassphrase123" not in content, content
    assert "TopSecretPasswordABC456" not in content, content


def test_passphrase_blocked_from_header_expansion(monkeypatch):
    """A passphrase must not silently expand into an HTTP header value."""
    monkeypatch.setenv("SSH_KEY_PASSPHRASE", "AnotherKeyPhrase456")
    assert se._is_denylisted("SSH_KEY_PASSPHRASE", frozenset())
