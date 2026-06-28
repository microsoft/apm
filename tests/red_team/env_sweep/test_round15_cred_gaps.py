"""Round-15 env breaks r15-env-1..3: wallet siblings, ODBC PWD=, rotation tails.

Round-14 closed MNEMONIC / SEED_PHRASE, the enumerated ``[_0-9]*`` digit tail,
and the ``password=`` / ``passwd=`` DSN keyword form. Three realistic siblings
still leaked the secret VALUE on the DEFAULT lifecycle path (no opt-in):

  * r15-env-1 (HIGH): wallet ``RECOVERY_PHRASE`` / ``SECRET_RECOVERY_PHRASE`` /
    ``BACKUP_PHRASE`` (the MetaMask / hardware-wallet spelling) and the curated
    ``*_SEED`` wallet names (``WALLET_SEED`` / ``MASTER_SEED`` /
    ``DERIVATION_SEED``). round-14 added MNEMONIC + SEED_PHRASE but not these.
    Double channel: 0600 scripts.log AND the post-install child env.
  * r15-env-2 (MED): the canonical Microsoft ODBC / SQL Server ``PWD=`` password
    keyword. round-14 deliberately excluded a bare ``pwd=`` to protect the shell
    ``PWD=/path`` echo -- but that carve-out also dropped a real DSN secret. The
    fix path-guards ``PWD=<value>``: masked unless the value is a filesystem path.
  * r15-env-3 (MED): rotation names with a trailing word / version tag
    (``PASSWORD_OLD`` / ``DB_PASS_OLD`` / ``DB_PASSV2``) escaped the digit-only
    ``[_0-9]*$`` anchor exactly as ``DB_PASS2`` did before round-14.

The fix extends the denylist alternation (``RECOVERY_PHRASE`` / ``BACKUP_PHRASE``
plus a rotation tail ``(?:_?(?:OLD|NEW|PREV|CURRENT))?(?:_?V[0-9]+)?``), curates
the wallet ``*_SEED`` names as blob names (so the legitimate RNG seeds
``PYTHONHASHSEED`` / ``RANDOM_SEED`` are NOT stripped from a build's child env),
and adds a path-guarded ``PWD=`` masker.

Each trap drives the REAL ``_append_to_script_log`` and ``_build_script_env``
paths with exact-value-absence assertions, and re-asserts the round-8/13/14
benign set plus the RNG-seed non-strip guard so the widened anchors cannot
over-match or break a reproducible build.
"""

from __future__ import annotations

import os

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry


@pytest.mark.parametrize(
    "name",
    [
        # r15-env-1 wallet recovery / backup phrases + curated *_SEED names
        "RECOVERY_PHRASE",
        "SECRET_RECOVERY_PHRASE",
        "WALLET_RECOVERY_PHRASE",
        "BACKUP_PHRASE",
        "WALLET_SEED",
        "MASTER_SEED",
        "DERIVATION_SEED",
        # r15-env-3 rotation names with a word / version tail
        "PASSWORD_OLD",
        "PASSWORD_NEW",
        "DB_PASS_OLD",
        "DB_PASS_PREV",
        "SECRET_CURRENT",
        "DB_PASSV2",
        "API_KEYV2",
        "TOKEN_OLD",
        # round-14 enumerated names must still match
        "DB_PASS2",
        "API_KEY2",
    ],
)
def test_round15_credential_names_recognised(name):
    """Every round-15 credential-name family must be denylisted."""
    assert se._matches_credential(name), name


@pytest.mark.parametrize(
    "benign",
    [
        # RNG / hashing seeds a build legitimately needs -- must NOT be stripped
        "PYTHONHASHSEED",
        "RANDOM_SEED",
        "TEST_SEED",
        "NUMPY_SEED",
        "TORCH_SEED",
        "DB_SEED_COUNT",
        # rotation words alone (no credential token) must stay benign
        "RENEW",
        "PREVIEW",
        "CURRENT",
        "VERSION",
        # round-8 benign set -- the rotation tail must not regress it
        "SURPASS",
        "BYPASS",
        "COMPASS",
        "PASSAGE",
        "PWD",
        "OLDPWD",
        "DATABASE",
        "RELEASE_BASE",
    ],
)
def test_round15_benign_names_not_over_matched(benign):
    """Widened rotation tail / wallet names must not sweep benign names."""
    assert not se._matches_credential(benign), benign


def test_r15_env_1_recovery_phrase_value_redacted_in_log(tmp_path, monkeypatch):
    """A wallet RECOVERY_PHRASE echoed to stdout must be masked in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv(
        "RECOVERY_PHRASE",
        "legal winner thank year wave sausage worth useful legal winner thank yellow",
    )
    se._append_to_script_log(
        "post-install",
        "command",
        "deploy",
        stdout="restoring legal winner thank year wave sausage worth useful legal winner thank yellow",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert (
        "legal winner thank year wave sausage worth useful legal winner thank yellow" not in content
    ), content
    assert "[REDACTED]" in content


def test_r15_env_1_wallet_seed_value_redacted_in_log(tmp_path, monkeypatch):
    """A curated WALLET_SEED value must not persist cleartext in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("WALLET_SEED", "0xdeadbeefcafef00d1122334455667788990011223344")
    se._append_to_script_log(
        "post-install",
        "command",
        "deploy",
        stdout="seed=0xdeadbeefcafef00d1122334455667788990011223344",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "0xdeadbeefcafef00d1122334455667788990011223344" not in content, content
    assert "[REDACTED]" in content


def test_r15_env_1_wallet_names_stripped_from_child_env(monkeypatch):
    """Wallet seed names must be removed from the lifecycle child env."""
    monkeypatch.setenv("RECOVERY_PHRASE", "abandon ability able about above absent absorb")
    monkeypatch.setenv("WALLET_SEED", "0xfeedfacefeedfacefeedfacefeedface")
    monkeypatch.setenv("BACKUP_PHRASE", "another twelve word phrase that backs up the wallet")
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert "RECOVERY_PHRASE" not in env
    assert "WALLET_SEED" not in env
    assert "BACKUP_PHRASE" not in env


def test_r15_env_1_rng_seed_preserved_in_child_env(monkeypatch):
    """A legitimate RNG seed must SURVIVE in the child env (reproducible build)."""
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    monkeypatch.setenv("RANDOM_SEED", "42")
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert env.get("PYTHONHASHSEED") == "0"
    assert env.get("RANDOM_SEED") == "42"


def test_r15_env_3_rotation_word_suffix_denylisted():
    """Rotation names with a word/version tail must not silently expand."""
    for name in ("PASSWORD_OLD", "DB_PASS_OLD", "DB_PASSV2", "API_KEYV2", "SECRET_CURRENT"):
        assert se._is_denylisted(name, frozenset()), name


def test_r15_env_3_rotation_value_redacted_in_log(tmp_path, monkeypatch):
    """A still-live rotated old password must not persist cleartext."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("PASSWORD_OLD", "oldRotatedSecret_4321xyz")
    se._append_to_script_log(
        "pre-install",
        "command",
        "rotate",
        stdout="old creds still valid: oldRotatedSecret_4321xyz",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert "oldRotatedSecret_4321xyz" not in content, content
    assert "[REDACTED]" in content


def test_r15_env_3_rotation_stripped_from_child_env(monkeypatch):
    """A rotation-named credential must be removed from the child env."""
    monkeypatch.setenv("PASSWORD_OLD", "oldRotatedSecret_4321xyz")
    monkeypatch.setenv("DB_PASSV2", "newRotatedSecret_8899abc")
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert "PASSWORD_OLD" not in env
    assert "DB_PASSV2" not in env


def test_r15_env_2_round8_benign_still_clean():
    """The widened matcher must not regress the canonical benign set."""
    for benign in ("SURPASS", "BYPASS", "COMPASS", "PASSAGE", "DATABASE"):
        assert not se._matches_credential(benign), benign
    # PWD/OLDPWD remain exempt regardless of the rotation-tail change.
    assert not se._is_denylisted("PWD", frozenset())
    assert not se._is_denylisted("OLDPWD", frozenset())
    # Sanity: os.environ is the real strip source for _build_script_env.
    assert isinstance(os.environ, os._Environ)
