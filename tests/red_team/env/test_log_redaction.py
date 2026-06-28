"""Vector 6: log redaction gaps and over-redaction.

``_redact_secrets`` masks the VALUE of env vars whose NAME matches the same
suffix-anchored denylist used everywhere else. So a script that echoes the
value of a suffix-gap secret (``GOOGLE_APPLICATION_CREDENTIALS``,
``AWS_ACCESS_KEY_ID``, plural forms) writes that secret in cleartext to
``~/.apm/logs/scripts.log``.

Conversely, a short (>=4 char) value of a correctly-denylisted var that also
happens to be a substring of ordinary log text is over-redacted, corrupting
unrelated output.

All tests route through the real ``_append_to_script_log`` with ``APM_HOME``
pointed at ``tmp_path`` so the real ``~/.apm`` is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.script_executors import _append_to_script_log, _redact_secrets

from .fixtures.helpers import read_script_log


class TestLogRedactionGaps:
    """Suffix-gap secret values must not persist in cleartext to the log."""

    def test_google_credentials_value_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ya29.cleartext-sa-token")
        out = _redact_secrets("creds=ya29.cleartext-sa-token")
        assert "ya29.cleartext-sa-token" not in out, (
            "GOOGLE_APPLICATION_CREDENTIALS value survived log redaction"
        )

    def test_aws_access_key_id_value_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIACLEARTEXT123456")
        out = _redact_secrets("key=AKIACLEARTEXT123456")
        assert "AKIACLEARTEXT123456" not in out

    def test_suffix_gap_secret_not_written_to_log_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        monkeypatch.setenv("FOO_TOKENS", "plural-token-cleartext")
        _append_to_script_log(
            "post-install",
            "command",
            "echo $FOO_TOKENS",
            stdout="emitted plural-token-cleartext to caller",
            exit_code=0,
        )
        log_text = read_script_log(tmp_path)
        assert "plural-token-cleartext" not in log_text, (
            "plural suffix-gap secret persisted in cleartext to scripts.log"
        )


class TestLogRedactionDefended:
    """A correctly-denylisted secret value IS redacted (regression trap)."""

    def test_blocked_secret_value_redacted_in_log_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        monkeypatch.setenv("ANALYTICS_TOKEN", "tok_blocked_value_xyz")
        _append_to_script_log(
            "post-install",
            "command",
            "echo $ANALYTICS_TOKEN",
            stdout="value is tok_blocked_value_xyz here",
            exit_code=0,
        )
        log_text = read_script_log(tmp_path)
        assert "tok_blocked_value_xyz" not in log_text
        assert "[REDACTED]" in log_text


class TestOverRedaction:
    """A short denylisted value that is a substring of normal text is over-masked."""

    def test_short_secret_does_not_corrupt_unrelated_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 4-char secret value that also appears inside ordinary words.
        monkeypatch.setenv("APP_TOKEN", "test")
        out = _redact_secrets("running tests in latest build")
        # Secure expectation: unrelated text is untouched (no false-positive mask).
        assert out == "running tests in latest build", (
            "over-redaction corrupted unrelated log text containing the secret substring"
        )
