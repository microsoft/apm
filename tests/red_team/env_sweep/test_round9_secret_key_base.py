"""Round-9 env regression trap: SECRET_KEY_BASE credential redaction.

r9-env-1 (MED) -- ``SECRET_KEY_BASE`` (the Rails master secret) carries the
credential tokens SECRET and KEY only as a PREFIX/INFIX; the
``_CREDENTIAL_DENYLIST`` alternation is anchored at the SUFFIX, so the name
ended in the benign ``_BASE`` tail and was never recognised. Its near-
identical sibling ``SECRET_KEY`` IS masked, so any command hook that
surfaced the value (a rails boot trace, ``rails runner 'puts ENV.to_h'``)
wrote the 41-char master secret to APM's own ``scripts.log`` in cleartext.
The fix curates the exact name into ``_CREDENTIAL_BLOB_NAMES`` (exact-match,
so KEYBASE_* / CODEBASE_* / DATABASE / RELEASE_BASE are unaffected).
"""

from __future__ import annotations

from apm_cli.core import script_executors as se

_SECRET = "s3cret_RAILS_MASTER_KEY_0123456789abcdef0"


def test_secret_key_base_recognised_as_credential():
    """SECRET_KEY_BASE must be treated as a credential (like SECRET_KEY)."""
    assert se._matches_credential("SECRET_KEY_BASE") is True
    assert se._matches_credential("SECRET_KEY") is True


def test_benign_base_siblings_not_over_matched():
    """The curated exact-name entry must not over-match benign siblings."""
    for benign in (
        "KEYBASE_USERNAME",
        "CODEBASE_PATH",
        "DATABASE",
        "RELEASE_BASE",
        "BASE_URL",
    ):
        assert se._matches_credential(benign) is False, benign


def test_secret_key_base_value_redacted_in_log(tmp_path, monkeypatch):
    """The SECRET_KEY_BASE value must not reach scripts.log in cleartext."""
    home = tmp_path / "home"
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.setenv("SECRET_KEY_BASE", _SECRET)
    monkeypatch.setenv("SECRET_KEY", "control_" + _SECRET)

    redacted = se._redact_secrets(f"stdout: SECRET_KEY_BASE={_SECRET}")

    assert _SECRET not in redacted
    assert "[REDACTED]" in redacted


def test_secret_key_base_blocked_from_header_expansion():
    """SECRET_KEY_BASE must be denylisted from http header expansion too."""
    assert se._is_denylisted("SECRET_KEY_BASE", frozenset()) is True
