"""Round-20 ENV-EXFILTRATION red-team sweep.

Probes a residual NAME-gap against the REAL credential machinery in
``apm_cli.core.script_executors``: keystore / PKCS#12 / PFX / JKS private-key
CONTAINER blobs. Each probe drives ``_matches_credential`` /
``_build_script_env`` / ``_expand_env_vars`` / ``_append_to_script_log`` /
``_redact_secrets`` and asserts the secure contract.

Secret-looking literals are assembled from fragments at call time so no
contiguous credential appears in source (GitHub push-protection safe).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry

# --- runtime-assembled fake secrets --------------------------------------- #
# A base64 of a binary Java keystore (.jks starts with the 0xFEEDFEED magic ->
# base64 "/u3+7Q") / PKCS#12 .pfx blob -- NOT PEM-armored, no "=" key, no URL,
# no "sig=", so NONE of the value-shape structural maskers (PEM, DSN password=,
# URL userinfo, webhook, SAS) can see it. Only a NAME match can protect it.
_JKS_B64 = "/u3+" + "7QAAAAIAAAAB" + "AAAABm15a2V5" + "U2lnbmluZ0tleVN0b3JlMTIzNDU2Nzg5"


# ========================================================================== #
# r20-env-1: keystore / PFX / P12 / JKS private-key CONTAINER name gap        #
# ========================================================================== #
#
# Android app signing and JVM / Windows-Authenticode code signing are among the
# most widely-deployed CI secret conventions. The signing key lives inside a
# binary keystore (.jks / .p12 / .pfx), which CI base64-encodes into ONE env var
# so it survives a flat secret store:
#   ANDROID_KEYSTORE_BASE64, SIGNING_KEY_STORE_BASE64, RELEASE_KEYSTORE,
#   WINDOWS_PFX_BASE64, APPLE_CERT_P12, SIGNING_JKS, ...
# The name carries the credential token "KEY" only as part of the compound word
# KEY+STORE -> the suffix-anchored denylist tail cannot consume "STORE", so the
# token never reaches "$"; PFX / P12 / JKS contain no denylist token at all; and
# none are in the curated blob-name / blob-suffix sets. So on the DEFAULT
# lifecycle path the base64-encoded private-key container:
#   (1) is NOT stripped from the command-script child env (exfil-to-subprocess),
#   (2) persists CLEARTEXT in the 0600 scripts.log when a script echoes its env,
#   (3) expands into an outbound HTTP header with no warning.
# The container is the actual signing private key -- disclosure = app/code
# signing compromise.

_LEAKY_KEYSTORE_NAMES = [
    "ANDROID_KEYSTORE_BASE64",
    "SIGNING_KEY_STORE_BASE64",
    "KEYSTORE_FILE_BASE64",
    "RELEASE_KEYSTORE",
    "UPLOAD_KEYSTORE",
    "SIGNING_KEYSTORE",
    "ANDROID_KEYSTORE",
    "WINDOWS_PFX_BASE64",
    "CODE_SIGNING_PFX",
    "APPLE_CERT_P12",
    "SIGNING_JKS",
    "SERVER_JKS_BASE64",
]


@pytest.mark.parametrize("name", _LEAKY_KEYSTORE_NAMES)
def test_r20_env_1_keystore_name_should_be_credential(name):
    """A base64 keystore/.pfx/.p12/.jks container NAME must be a credential.

    SECURE CONTRACT (post-fix): the keystore container regex on
    ``_CREDENTIAL_BLOB_SUFFIX`` recognizes every keystore/PKCS#12 container
    name, so ``_matches_credential`` returns True and the private key is
    stripped/redacted everywhere.
    """
    assert se._matches_credential(name) is True


def test_r20_env_1_keystore_leaks_into_child_env(monkeypatch):
    """SECURE (a): the keystore container is stripped from the child env."""
    monkeypatch.setenv("ANDROID_KEYSTORE_BASE64", _JKS_B64)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert "ANDROID_KEYSTORE_BASE64" not in env


def test_r20_env_1_keystore_persists_cleartext_in_log(tmp_path, monkeypatch):
    """SECURE (c): the base64 keystore is REDACTED from scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("SIGNING_JKS", _JKS_B64)
    se._append_to_script_log(
        "post-install",
        "command",
        "sign-app",
        stdout=f"SIGNING_JKS={_JKS_B64}",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert _JKS_B64 not in content


def test_r20_env_1_keystore_expands_into_outbound_header(monkeypatch):
    """SECURE (b): the keystore is REFUSED for $VAR header expansion."""
    monkeypatch.setenv("WINDOWS_PFX_BASE64", _JKS_B64)
    expanded = se._expand_env_vars("$WINDOWS_PFX_BASE64", frozenset())
    assert expanded == ""


# ========================================================================== #
# FP-safety: a fix must NOT strip the benign keystore PATH / alias vars       #
# ========================================================================== #
#
# The keystore *file path* / alias / store-type vars are not secrets (like
# KUBECONFIG / PWD); a fix that strips them would break the very signing step.
# These must stay False so the build keeps working.
@pytest.mark.parametrize(
    "name",
    [
        "ANDROID_KEYSTORE_PATH",
        "KEYSTORE_PATH",
        "KEYSTORE_FILE",
        "KEYSTORE_ALIAS",
        "TRUSTSTORE_PATH",
    ],
)
def test_r20_env_1_benign_keystore_path_vars_stay(name):
    """Benign keystore path/alias vars must NOT be treated as credentials."""
    assert se._matches_credential(name) is False, name


# A benign encoded asset with no credential token must still reach the child
# env (guards against an over-broad fix re-introducing the r19 FP class).
@pytest.mark.parametrize("name", ["IMAGE_BASE64", "LOGO_B64", "FONT_BASE64", "COLOR_HEX"])
def test_r20_env_1_benign_encoded_assets_stay(name):
    assert se._matches_credential(name) is False, name
