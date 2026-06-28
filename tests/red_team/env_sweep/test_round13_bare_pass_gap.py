"""Round-13 env break r13-env-1: bare ``*_PASS`` credential-name gap.

The credential denylist alternation carried ``PASSWORD`` / ``PASSWD`` /
``PASSPHRASE`` / ``PWD`` but NOT the bare ``PASS`` shorthand. Real-world
names like ``DB_PASS``, ``MYSQL_PASS``, ``REDIS_PASS``, ``SMTP_PASS``,
``ROOT_PASS`` (ubiquitous in docker-compose / 12-factor stacks) matched
neither ``_CREDENTIAL_DENYLIST`` nor ``_CREDENTIAL_BLOB_NAMES``, causing a
double exposure:

  1. ``_build_script_env`` did not strip the var -> the cleartext secret
     was handed to the child subprocess environment.
  2. ``_redact_secrets`` built no needle for it -> when the script echoed
     ``$DB_PASS`` the value persisted CLEARTEXT in the 0600 scripts.log.

The fix anchors a bare ``PASS`` token to a ``_``-or-start boundary
(``(?:^|_)PASS``) so the real ``*_PASS`` family is swept WITHOUT catching
the common-English suffix words SURPASS / BYPASS / COMPASS / PASSAGE that
the round-8 trap guards as benign.

These traps drive the REAL ``_append_to_script_log`` path and read the
on-disk scripts.log back, plus a unit-level ``_redact_secrets`` /
``_matches_credential`` assertion and a denylist-strip check, asserting the
secret VALUE never appears in cleartext and the var never reaches the child
env. The ``PASSWORD``-named sibling under the identical echo is the masked
control, proving a name-gap flaw rather than a redaction-mechanism flaw.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASS",
        "MYSQL_PASS",
        "REDIS_PASS",
        "SMTP_PASS",
        "ROOT_PASS",
        "APP_PASS",
        "DATABASE_PASS",
        "PASS",
        "db_pass",
    ],
)
def test_bare_pass_names_recognised(name):
    """A ``*_PASS`` / bare ``PASS`` name is the same secret class as PASSWORD."""
    assert se._matches_credential(name)


@pytest.mark.parametrize(
    "benign",
    [
        "SURPASS",
        "BYPASS",
        "COMPASS",
        "ENCOMPASS",
        "OVERPASS",
        "UNDERPASS",
        "TRESPASS",
        "PASSAGE",
        "PASSENGER_COUNT",
        "COMPASS_HEADING",
        "PWD",
        "OLDPWD",
    ],
)
def test_english_suffix_words_not_over_matched(benign):
    """Common words that merely contain ``PASS`` must NOT be denylisted."""
    assert not se._matches_credential(benign)


def test_passworded_siblings_still_recognised():
    """The boundary anchor must not regress the long PASSWORD/APIKEY forms."""
    for name in ("DB_PASSWORD", "PASSWORD", "MYPASSWORD", "APIKEY", "GPG_PASSPHRASE"):
        assert se._matches_credential(name), name


def test_bare_pass_value_redacted_in_log(tmp_path, monkeypatch):
    """A ``DB_PASS`` echoed to stdout must be masked, like its PASSWORD sibling."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("DB_PASS", "hunter2-prod-db-xyz")
    monkeypatch.setenv("DB_PASSWORD", "control-secret-abc987")
    se._append_to_script_log(
        "post-install",
        "command",
        "deploy",
        stdout="db=hunter2-prod-db-xyz pw=control-secret-abc987",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "hunter2-prod-db-xyz" not in content, content
    assert "control-secret-abc987" not in content, content
    assert "[REDACTED]" in content


def test_bare_pass_stripped_from_child_env(monkeypatch):
    """A ``*_PASS`` var must not silently expand into the child process env."""
    monkeypatch.setenv("MYSQL_PASS", "AnotherProdSecret456")
    assert se._is_denylisted("MYSQL_PASS", frozenset())


def test_unit_bare_pass_value_masked(monkeypatch):
    """Unit-level: the redactor masks a ``*_PASS`` value in arbitrary text."""
    monkeypatch.setenv("REDIS_PASS", "RedisProdSecret7788")
    out = se._redact_secrets("connecting with RedisProdSecret7788 now")
    assert "RedisProdSecret7788" not in out
    assert "[REDACTED]" in out
