"""Round-19 ENV-EXFILTRATION red-team sweep.

Probes residual NAME-gap, structural-masker, and log-forgery vectors against the
REAL credential machinery in ``apm_cli.core.script_executors``. Each probe drives
``_matches_credential`` / ``_build_script_env`` / ``_expand_env_vars`` /
``_append_to_script_log`` / ``_redact_secrets`` and asserts the secure contract.

Secret-looking literals are assembled from fragments at call time so no
contiguous credential appears in source (GitHub push-protection safe).
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry

# --- runtime-assembled fake secrets --------------------------------------- #
# A long base64-looking blob (a CI-encoded service-account key / kubeconfig).
_B64_SECRET = "ZXlK" + "aGJHY2lPaUpJ" + "Uzl0c2VjcmV0" + "VmFsdWUxMjM0NQ=="
_OPAQUE = "Sec" + "ret" + "Value" + "0123456789ABCDEF"


# ========================================================================== #
# r19-env-1: *_BASE64 / *_B64 encoded-secret env-var NAME gap                 #
# ========================================================================== #
#
# CI pipelines routinely base64-encode a whole secret (a GCP service-account
# JSON, a kubeconfig, a TLS private key) into ONE env var named ``<x>_BASE64``
# / ``<x>_B64`` so it survives a single-line secret store. The credential token
# (KEY/TOKEN/SECRET/CREDENTIALS) is an INFIX; the name ENDS in the benign
# ``BASE64`` encoding tail, which the suffix-anchored denylist cannot express
# and which is not in the curated blob-name set. So on the DEFAULT lifecycle
# path the encoded secret:
#   (1) is NOT stripped from the command-script child env (exfil-to-subprocess),
#   (2) persists CLEARTEXT in the 0600 scripts.log when a script echoes its env,
#   (3) expands into an outbound HTTP header with no warning.


@pytest.mark.parametrize(
    "name",
    [
        "GCP_SA_KEY_BASE64",
        "GOOGLE_CREDENTIALS_BASE64",
        "KUBE_CONFIG_BASE64",
        "TLS_PRIVATE_KEY_BASE64",
        "JWT_SECRET_B64",
        "API_TOKEN_BASE64",
    ],
)
def test_r19_env_1_base64_secret_name_is_credential(name):
    """A ``<credential-token>_BASE64`` / ``_B64`` name must be a credential."""
    assert se._matches_credential(name) is True, name


def test_r19_env_1_base64_secret_redacted_in_log(tmp_path, monkeypatch):
    """The base64-encoded secret must NOT persist cleartext in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("GCP_SA_KEY_BASE64", _B64_SECRET)
    se._append_to_script_log(
        "post-install",
        "command",
        "gcloud-auth",
        stdout=f"GCP_SA_KEY_BASE64={_B64_SECRET}",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert _B64_SECRET not in content, content


def test_r19_env_1_base64_secret_stripped_from_child_env(monkeypatch):
    """The base64-encoded secret must NOT reach the command-script child env."""
    monkeypatch.setenv("GCP_SA_KEY_BASE64", _B64_SECRET)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert "GCP_SA_KEY_BASE64" not in env


def test_r19_env_1_base64_secret_blocked_from_header_expansion(monkeypatch):
    """``$..._BASE64`` must be refused for HTTP-header $VAR expansion."""
    monkeypatch.setenv("GCP_SA_KEY_BASE64", _B64_SECRET)
    expanded = se._expand_env_vars("Authorization: Bearer $GCP_SA_KEY_BASE64")
    assert _B64_SECRET not in expanded


def test_r19_env_1_benign_base64_asset_not_overstripped(monkeypatch):
    """A token-less ``IMAGE_BASE64`` asset must still reach the child env (no FP)."""
    monkeypatch.setenv("IMAGE_BASE64", _B64_SECRET)
    monkeypatch.setenv("LOGO_B64", _B64_SECRET)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert env.get("IMAGE_BASE64") == _B64_SECRET
    assert env.get("LOGO_B64") == _B64_SECRET


# ========================================================================== #
# Clean-vector controls (these SHOULD pass -- confirm no regression/FP)       #
# ========================================================================== #


@pytest.mark.parametrize(
    "name",
    [
        "CARGO_REGISTRIES_MYREG_TOKEN",
        "HF_TOKEN",
        "VAULT_TOKEN",
        "DIGITALOCEAN_ACCESS_TOKEN",
        "NPM_CONFIG__AUTH",
        "DOCKER_AUTH_CONFIG",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "TWILIO_AUTH_TOKEN",
        "STRIPE_SECRET_KEY",
        "NGROK_AUTHTOKEN",
        "RAILS_MASTER_KEY",
    ],
)
def test_r19_known_credential_names_matched(name):
    assert se._matches_credential(name) is True, name


@pytest.mark.parametrize(
    "name",
    ["TF_VAR_region", "TF_CLI_ARGS_init", "TF_LOG", "PWD", "OLDPWD", "DATABASE", "CODEBASE"],
)
def test_r19_benign_names_not_matched(name):
    assert se._matches_credential(name) is False, name
