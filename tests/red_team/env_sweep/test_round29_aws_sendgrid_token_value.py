"""Round-29 env red-team: AWS access-key and SendGrid API-key VALUE leaks.

Two of the most widely flagged real-world credential shapes carry a fixed
provider prefix yet are matched by NO arm of ``_PROVIDER_TOKEN_PATTERN`` and
by none of the structural value maskers (URL-cred, conn-string, webhook, SAS,
PEM, JWT), so a lifecycle script that prints them to stdout/stderr persists
the cleartext credential in ``~/.apm/logs/scripts.log``.

1. AWS access key id -- ``AKIA``/``ASIA`` + 16 uppercase-base32 chars.
   A post-install bootstrap that runs ``aws configure export-credentials``,
   ``aws sts assume-role`` or ``aws sts get-session-token`` prints a FRESH,
   just-minted credential pair to stdout. Because the credential was minted
   by the CLI it is NOT in ``os.environ`` (and a temporary ``ASIA`` STS key
   never could be), so the name/value channel in ``_redact_secrets`` -- which
   only masks the VALUES of denylisted *env vars* -- never sees it. The
   ``AKIA``/``ASIA`` access-key id is the canonical AWS credential marker:
   GitHub push-protection and AWS's own secret-scanning treat it as a leaked
   secret. It has no ``@`` userinfo, no ``password=`` key, no double-``eyJ``
   JWT shape, and no enumerated provider prefix, so it survives redaction.

2. SendGrid API key -- ``SG.<22>.<43>``. A self-contained bearer with full
   send/read authority over an account, printed by SendGrid setup tooling and
   echoed by ``@sendgrid/mail`` bootstrap scripts. The whole string is the
   secret, it has a fixed ``SG.`` prefix and a rigid two-dot shape, yet no
   masker arm covers it -- the JWT masker requires a double-``eyJ`` anchor.

These tests assert the SECURE behaviour (credential absent from the redaction
output AND from the on-disk log). They FAIL on HEAD 427ed91de, proving the
gap. Token-shaped strings are assembled at runtime -- never real literals.
"""

from __future__ import annotations

import os
import secrets
import string
import threading

import pytest

from apm_cli.core import script_executors
from apm_cli.core.script_executors import _append_to_script_log, _redact_secrets


def _watchdog(seconds: float = 20.0):
    """Fail fast on a hang without using a shell timeout (banned)."""

    def _boom():
        os._exit(99)  # pragma: no cover - only fires on a genuine hang

    t = threading.Timer(seconds, _boom)
    t.daemon = True
    t.start()
    return t


def _fake_aws_access_key(prefix: str) -> str:
    """Build an AWS-shaped access key id at runtime (prefix + 16 base32 chars)."""
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(16))
    return prefix + body


def _fake_sendgrid_key() -> str:
    """Build a SendGrid-shaped API key at runtime (SG.<22>.<43>)."""
    alphabet = string.ascii_letters + string.digits + "_-"
    head = "".join(secrets.choice(alphabet) for _ in range(22))
    tail = "".join(secrets.choice(alphabet) for _ in range(43))
    return f"SG.{head}.{tail}"


# ---------------------------------------------------------------------------
# Finding r29-env-1: AWS access key id (AKIA / ASIA)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["AKIA", "ASIA"])
def test_round29_aws_access_key_redacted_from_output(prefix):
    """An AWS access key id echoed to stdout must not survive _redact_secrets."""
    wd = _watchdog()
    try:
        key_id = _fake_aws_access_key(prefix)
        # Simulate `aws configure export-credentials --format env` stdout.
        stdout = (
            "export AWS_ACCESS_KEY_ID=" + key_id + "\nexport AWS_SESSION_TOKEN=FQoG...truncated\n"
        )
        redacted = _redact_secrets(stdout)
        assert key_id not in redacted, (
            f"AWS {prefix} access key id leaked through _redact_secrets: "
            "cleartext credential present in redaction output"
        )
    finally:
        wd.cancel()


def test_round29_aws_access_key_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real log writer must not persist an AWS access key id."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        # Freshly minted STS credential -- ensure nothing backs it in env.
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(var, raising=False)

        key_id = _fake_aws_access_key("ASIA")
        _append_to_script_log(
            "postinstall",
            "shell",
            "aws sts get-session-token --output text",
            stdout=f"CREDENTIALS\t{key_id}\t2026-01-01T00:00:00Z\twJalr...\n",
            status="ok",
            exit_code=0,
        )

        log_path = script_executors._get_scripts_log_path()
        contents = log_path.read_text(encoding="utf-8")
        assert key_id not in contents, (
            "AWS access key id persisted in cleartext in ~/.apm/logs/scripts.log; "
            f"found leaked credential in log file at {log_path}"
        )
    finally:
        wd.cancel()


# ---------------------------------------------------------------------------
# Finding r29-env-2: SendGrid API key (SG.<22>.<43>)
# ---------------------------------------------------------------------------


def test_round29_sendgrid_key_redacted_from_output():
    """A SendGrid API key echoed to stdout must not survive _redact_secrets."""
    wd = _watchdog()
    try:
        key = _fake_sendgrid_key()
        stdout = f"Your SendGrid API key: {key}\n"
        redacted = _redact_secrets(stdout)
        assert key not in redacted, (
            "SendGrid API key leaked through _redact_secrets: "
            "cleartext bearer present in redaction output"
        )
    finally:
        wd.cancel()


def test_round29_sendgrid_key_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real log writer must not persist a SendGrid API key."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)

        key = _fake_sendgrid_key()
        _append_to_script_log(
            "postinstall",
            "shell",
            "node scripts/print-sendgrid-key.js",
            stdout=f"SENDGRID_API_KEY={key}\n",
            status="ok",
            exit_code=0,
        )

        log_path = script_executors._get_scripts_log_path()
        contents = log_path.read_text(encoding="utf-8")
        assert key not in contents, (
            "SendGrid API key persisted in cleartext in ~/.apm/logs/scripts.log; "
            f"found leaked bearer in log file at {log_path}"
        )
    finally:
        wd.cancel()


# ---------------------------------------------------------------------------
# False-positive guard: the AWS / SendGrid arms must NOT over-redact benign
# look-alikes. The AWS arm requires exactly 16 [A-Z0-9] AFTER ``AKIA``/``ASIA``
# with no separator; the SendGrid arm requires the rigid ``SG.<22>.<43>`` shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "benign",
    [
        "arn:aws:iam::123456789012:role/DeployRole",  # ARN, no AKIA/ASIA marker
        "Region us-east-1 selected for ASIA-PACIFIC rollout",  # 'ASIA' word + sep
        "ASIATIC_RUGS_CATALOG_V2",  # 'ASIA' + underscore breaks the 16-char run
        "export AWS_DEFAULT_REGION=ap-southeast-1",  # plain region, no key id
        "SG.Mail.Helpers.MailSettings.SandboxMode",  # dotted .NET module path
        "build SG.1.0 release notes",  # short dotted version, not <22>.<43>
    ],
)
def test_round29_benign_lookalikes_not_over_redacted(benign):
    """Benign AWS/SendGrid-shaped strings must survive _redact_secrets intact."""
    wd = _watchdog()
    try:
        assert _redact_secrets(benign) == benign, (
            f"benign look-alike over-redacted by the round-29 arms: {benign!r}"
        )
    finally:
        wd.cancel()
