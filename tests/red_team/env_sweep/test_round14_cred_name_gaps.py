"""Round-14 env breaks r14-env-1..4: credential-name gaps + DSN password form.

The round-13 ``*_PASS`` hardening still left four realistic credential
families leaking their VALUE both to the lifecycle child env and to the
0600 ``scripts.log``:

  * r14-env-1 (HIGH): ``MNEMONIC`` / ``*_MNEMONIC`` / ``*_SEED_PHRASE`` -- the
    web3 wallet seed phrase ubiquitous in hardhat / foundry / truffle deploy
    lifecycle scripts. A single leaked token drains a wallet.
  * r14-env-2 (MED): ``NPM_CONFIG__AUTH`` (the actual ``npm_config__auth`` env
    name) and other bare ``*_AUTH`` blobs carrying base64 ``user:pass`` -- the
    curated ``NPM_AUTH`` / ``REGISTRY_AUTH`` names showed intent but missed the
    real variable names.
  * r14-env-3 (MED): the round-13 ``(?:^|_)PASS...$`` anchor was end-anchored,
    so an enumerated / rotated credential (``DB_PASS2``, ``PASS_1``,
    ``TOKEN_1``, ``API_KEY2``) defeated the sweep with a trailing digit.
  * r14-env-4 (MED): connection-string / DSN shorthands (``DSN``, ``CONN_STR``,
    ``*_DSN``) were not recognised by NAME, and the in-text redactor only
    masked URL ``scheme://user:pass@`` userinfo -- never the libpq / JDBC
    ``password=<value>`` key=value form.

The fixes extend the denylist alternation (``MNEMONIC`` / ``SEED_PHRASE`` plus
a trailing ``[_0-9]*`` for enumerated names), the blob-name / suffix sets
(``DSN`` / ``CONN_STR`` / ``_AUTH`` / ``_DSN`` / ``_CONN_STR``), and add a
NAME-independent ``password=`` connection-string masker.

Each trap drives the REAL ``_append_to_script_log`` path and reads the on-disk
scripts.log back (or the unit-level ``_matches_credential`` /
``_redact_secrets`` / ``_is_denylisted`` predicates), asserting the secret
VALUE never appears in cleartext and the var never reaches the child env. The
round-8 benign set is re-asserted so the widened anchor cannot over-match
common English words.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se


@pytest.mark.parametrize(
    "name",
    [
        # r14-env-1 wallet seed phrases
        "MNEMONIC",
        "WALLET_MNEMONIC",
        "ETH_MNEMONIC",
        "DEPLOYER_MNEMONIC",
        "SEED_PHRASE",
        "DEPLOY_SEED_PHRASE",
        # r14-env-2 npm / registry auth blobs
        "NPM_CONFIG__AUTH",
        "ARTIFACTORY_AUTH",
        "PYPI_AUTH",
        # r14-env-3 enumerated / rotated credentials
        "DB_PASS2",
        "PASS_1",
        "TOKEN_1",
        "API_KEY2",
        "SECRET_0",
        "GH_TOKEN_2",
        # r14-env-4 DSN / connection-string names
        "DSN",
        "CONN_STR",
        "DATABASE_DSN",
        "SENTRY_DSN",
        "APP_CONN_STR",
    ],
)
def test_round14_credential_names_recognised(name):
    """Every round-14 credential-name family must be denylisted."""
    assert se._matches_credential(name), name


@pytest.mark.parametrize(
    "benign",
    [
        # round-8 benign set -- the widened [_0-9]* anchor must not regress it
        "SURPASS",
        "BYPASS",
        "COMPASS",
        "TRESPASS",
        "PASSAGE",
        "PASSENGER_COUNT",
        "PWD",
        "OLDPWD",
        # path / generic vars that merely resemble a token
        "PATH",
        "TRACE_ID",
        "HOME",
        "DATABASE",
        "RELEASE_BASE",
        # OAuth client config name does not end in a bare _AUTH token
        "GITHUB_OAUTH",
    ],
)
def test_round14_benign_names_not_over_matched(benign):
    """Widened anchors/suffixes must not sweep benign names."""
    assert not se._matches_credential(benign), benign


def test_mnemonic_value_redacted_in_log(tmp_path, monkeypatch):
    """A web3 MNEMONIC echoed to stdout must be masked in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("MNEMONIC", "abandon ability able about above absent absorb abstract")
    monkeypatch.setenv("DB_PASS", "control-pass-secret-9988")
    se._append_to_script_log(
        "post-install",
        "command",
        "deploy",
        stdout="seed=abandon ability able about above absent absorb abstract pw=control-pass-secret-9988",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "abandon ability able about above absent absorb abstract" not in content, content
    assert "control-pass-secret-9988" not in content, content
    assert "[REDACTED]" in content


def test_npm_config_auth_value_redacted_in_log(tmp_path, monkeypatch):
    """NPM_CONFIG__AUTH base64 user:pass must not persist cleartext."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("NPM_CONFIG__AUTH", "aGVsbG86c3VwZXJzZWNyZXR0b2tlbjEyMzQ=")
    se._append_to_script_log(
        "pre-install",
        "command",
        "auth",
        stdout="npm auth -> aGVsbG86c3VwZXJzZWNyZXR0b2tlbjEyMzQ=",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "aGVsbG86c3VwZXJzZWNyZXR0b2tlbjEyMzQ=" not in content, content
    assert "[REDACTED]" in content


def test_enumerated_pass_stripped_from_child_env(monkeypatch):
    """An enumerated ``*_PASS2`` var must not silently expand into the child env."""
    assert se._is_denylisted("DB_PASS2", frozenset())
    assert se._is_denylisted("PASS_1", frozenset())


def test_dsn_keyword_password_masked_name_independent():
    """A libpq/JDBC ``password=<value>`` is masked regardless of var NAME."""
    text = "stdout: DATABASE_URL host=db user=admin password=dsnSecret_9911Qz dbname=app"
    out = se._redact_connection_string_password(text)
    assert "dsnSecret_9911Qz" not in out, out
    assert "[REDACTED]" in out
    # The benign non-secret fields survive.
    assert "dbname=app" in out
    assert "user=admin" in out


def test_conn_string_password_does_not_corrupt_pwd_path():
    """The ``PWD=/path`` shell echo must NOT be mistaken for a ``pwd=`` secret."""
    text = "PWD=/home/user/project/passwords env dumped"
    assert se._redact_connection_string_password(text) == text


def test_dsn_value_redacted_in_log(tmp_path, monkeypatch):
    """A DSN env var carrying a keyword password is masked end-to-end."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("DSN", "host=db user=admin password=dsnLogSecret_7711 dbname=app")
    se._append_to_script_log(
        "post-install",
        "command",
        "migrate",
        stdout="connecting host=db user=admin password=dsnLogSecret_7711 dbname=app",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "dsnLogSecret_7711" not in content, content
    assert "[REDACTED]" in content
