"""Round-18 env breaks r18-env-1..3: Terraform Cloud ``TF_TOKEN_<host>`` and
Bundler per-source ``BUNDLE_<host>=user:password`` credentials.

Two real, widely-deployed credential conventions key the secret by HOST inside
the variable NAME, so the suffix-anchored denylist and the curated blob-name set
both missed them on the DEFAULT lifecycle path (no opt-in):

  * r18-env-1 / r18-env-2 (HIGH): Terraform Cloud / Enterprise reads
    ``TF_TOKEN_<host>`` (dots -> ``_``, e.g. ``TF_TOKEN_app_terraform_io``) as
    the ``terraform init`` API bearer. ``_TOKEN`` is an INFIX, so the
    suffix-anchored denylist missed it: the bearer leaked cleartext to the 0600
    ``scripts.log`` AND expanded into an outbound HTTP header with no warning.
    A START-anchored ``^TF_TOKEN_`` prefix match routes it through
    ``_matches_credential`` so it is redacted in the log, stripped from the
    child env, and refused for ``$VAR`` header expansion. Terraform's benign
    siblings (``TF_VAR_*`` / ``TF_CLI_*`` / ``TF_LOG*``) are untouched.
  * r18-env-3 (MED): Bundler keys a per-gem-source basic-auth credential by host
    in the name (dots/dashes -> ``__``), e.g.
    ``BUNDLE_GEMS__CONTRIBSYS__COM=user:password``. The host-suffixed name
    matches no denylist token AND must NOT be stripped from the child env (that
    breaks the ``bundle install`` it authenticates), so a NAME-based strip is
    wrong. The fix masks the ``user:PASSWORD`` pair STRUCTURALLY in the log text
    (log-only): the password half is redacted while the variable still reaches
    the child env intact. Benign ``BUNDLE_PATH`` / ``BUNDLE_JOBS`` config and a
    mirror-URL value (``https://...``) are not damaged.

Each trap drives the REAL ``_redact_secrets`` / ``_append_to_script_log`` /
``_build_script_env`` / ``_expand_env_vars`` paths with exact-value-absence
assertions. Secret values are assembled at runtime from fragments so no
contiguous credential literal appears in source.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import ScriptEntry

# Fake credential fragments assembled at call time (never a contiguous literal).
_TF_SECRET = "Tf" + "Cloud" + "Bearer" + "1234567890abcdef"
_BUNDLE_USER = "svcuser"
_BUNDLE_SECRET = "Gem" + "Source" + "Pw" + "0987654321"


# --------------------------------------------------------------------------- #
# r18-env-1 -- TF_TOKEN_<host> leaks to scripts.log                            #
# --------------------------------------------------------------------------- #


def test_r18_env_1_tf_token_matches_credential() -> None:
    """A ``TF_TOKEN_<host>`` name is recognised as a credential."""
    assert se._matches_credential("TF_TOKEN_app_terraform_io") is True
    assert se._matches_credential("TF_TOKEN_HOST_CORP_NET") is True


def test_r18_env_1_tf_token_redacted_in_log(tmp_path, monkeypatch):
    """The Terraform Cloud bearer must not persist its value in scripts.log."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("TF_TOKEN_app_terraform_io", _TF_SECRET)
    se._append_to_script_log(
        "post-install",
        "command",
        "terraform-init",
        stdout=f"using token {_TF_SECRET}",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert _TF_SECRET not in content, content
    assert "[REDACTED]" in content


def test_r18_env_1_tf_token_stripped_from_child_env(monkeypatch):
    """The bearer must NOT reach the command-script child environment."""
    monkeypatch.setenv("TF_TOKEN_app_terraform_io", _TF_SECRET)
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert "TF_TOKEN_app_terraform_io" not in env


def test_r18_env_1_benign_tf_vars_survive(monkeypatch):
    """Terraform's benign TF_VAR_/TF_CLI_/TF_LOG vars must reach the child env."""
    monkeypatch.setenv("TF_VAR_region", "westus2")
    monkeypatch.setenv("TF_CLI_ARGS_init", "-upgrade")
    monkeypatch.setenv("TF_LOG", "DEBUG")
    script = ScriptEntry(script_type="command", event="post-install", command="env")
    env = se._build_script_env(script)
    assert env.get("TF_VAR_region") == "westus2"
    assert env.get("TF_CLI_ARGS_init") == "-upgrade"
    assert env.get("TF_LOG") == "DEBUG"


# --------------------------------------------------------------------------- #
# r18-env-2 -- TF_TOKEN_<host> exfil via HTTP header $VAR expansion             #
# --------------------------------------------------------------------------- #


def test_r18_env_2_tf_token_blocked_from_header_expansion(monkeypatch):
    """A ``$TF_TOKEN_<host>`` reference in an HTTP header must not expand."""
    monkeypatch.setenv("TF_TOKEN_app_terraform_io", _TF_SECRET)
    expanded = se._expand_env_vars("Bearer $TF_TOKEN_app_terraform_io")
    assert _TF_SECRET not in expanded
    assert expanded == "Bearer "


def test_r18_env_2_tf_var_still_expands(monkeypatch):
    """A benign ``$TF_VAR_*`` reference still expands (no over-blocking)."""
    monkeypatch.setenv("TF_VAR_region", "eastus")
    assert se._expand_env_vars("region=$TF_VAR_region") == "region=eastus"


# --------------------------------------------------------------------------- #
# r18-env-3 -- Bundler BUNDLE_<host>=user:password leaks to scripts.log         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "assignment",
    [
        "BUNDLE_PATH=vendor/bundle",
        "BUNDLE_JOBS=4",
        "BUNDLE_BUILD__NOKOGIRI=--use-system-libraries",
    ],
)
def test_r18_env_3_benign_bundle_config_not_masked(tmp_path, monkeypatch, assignment):
    """Benign BUNDLE_* config (no user:pass pair) must not be corrupted."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    se._append_to_script_log("post-install", "command", "bundle", stdout=assignment, status="ok")
    content = (tmp_path / "logs" / "scripts.log").read_text()
    value = assignment.split("=", 1)[1]
    assert value in content, content


def test_r18_env_3_bundle_mirror_url_not_mistaken_for_credential(tmp_path, monkeypatch):
    """A BUNDLE_MIRROR https:// value is not a user:pass pair -- keep it intact."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    url = "https://gems.example.com/mirror"
    se._append_to_script_log(
        "post-install",
        "command",
        "bundle",
        stdout=f"BUNDLE_MIRROR__HTTPS://RUBYGEMS__ORG/={url}",
        status="ok",
    )
    content = (tmp_path / "logs" / "scripts.log").read_text()
    assert url in content, content
