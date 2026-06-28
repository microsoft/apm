"""Round-30 env red-team: Shopify + Stripe-test credential VALUE leaks.

The NAME denylist class is exhausted, so this round pivots to VALUE-shape gaps
in ``_PROVIDER_TOKEN_PATTERN`` -- the name-independent structural masker that
catches a raw provider secret printed to stdout/stderr with NO backing
credential-named env var (so the os.environ value-redactor never sees it) and
no surrounding structural delimiter (no ``@`` userinfo, no ``password=`` key,
no double-``eyJ`` JWT shape).

Two widely-deployed, fixed-prefix provider credentials are matched by NO arm of
the current pattern and by none of the structural value maskers:

1. Shopify Admin API token family -- ``shpat_`` (admin access token),
   ``shpss_`` (shared secret), ``shpca_`` (custom-app token), ``shppa_``
   (legacy private-app password), each followed by 32 hex chars. The Shopify
   CLI (``shopify app env show``, ``shopify theme info``) prints these to
   stdout during an app/theme deploy lifecycle, and an installed access token
   is frequently obtained at runtime (no env var backs it). The ``shp**_`` +
   32-hex shape is a rigid, zero-false-positive anchor: nothing benign matches
   it. GitHub push-protection and Shopify's own secret-scanning treat it as a
   leaked secret.

2. Stripe TEST-mode secret / restricted keys -- ``sk_test_`` / ``rk_test_``.
   The pattern already masks Stripe LIVE keys (``[sr]k_live_``), so the project
   has already decided Stripe secret keys are worth masking; the test-mode
   variants sit in the SAME "Secret keys" section of the Stripe dashboard and
   carry full test-mode account authority (create/read customers, charges,
   payment intents, read account metadata). ``stripe config --list`` and
   ``stripe listen`` print the test secret key to stdout in a payments
   bootstrap lifecycle. Masking live but not test is an inconsistency, not a
   scope decision.

These tests assert the SECURE contract: the credential VALUE is ABSENT from the
redaction output AND from the on-disk 0600 scripts.log, while benign
look-alikes survive unredacted. They FAIL on HEAD 059f2f4e2, proving the gap.
Token-shaped strings are assembled at runtime -- never real literals -- so
push-protection cannot block the probe.
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


def _hex(n: int) -> str:
    return "".join(secrets.choice("0123456789abcdef") for _ in range(n))


def _b62(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _fake_shopify_token(prefix: str) -> str:
    """Build a Shopify-shaped token at runtime (prefix + 32 hex chars)."""
    return prefix + "_" + _hex(32)


def _fake_stripe_test_key(prefix: str) -> str:
    """Build a Stripe test-mode secret/restricted key at runtime."""
    return prefix + "_test_" + _b62(24)


# ---------------------------------------------------------------------------
# Finding r30-env-1: Shopify Admin API token family (shpat_ / shpss_ / ...)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["shpat", "shpss", "shpca", "shppa"])
def test_round30_shopify_token_redacted_from_output(prefix):
    """A Shopify Admin API token echoed to stdout must not survive redaction."""
    wd = _watchdog()
    try:
        token = _fake_shopify_token(prefix)
        # Simulate `shopify app env show` stdout (token has no backing env var).
        stdout = "SHOP=acme.myshopify.com\nAccess token: " + token + "\n"
        redacted = _redact_secrets(stdout)
        assert token not in redacted, (
            f"Shopify {prefix}_ token leaked through _redact_secrets: "
            "cleartext credential present in redaction output"
        )
    finally:
        wd.cancel()


def test_round30_shopify_token_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real log writer must not persist a Shopify access token."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        # Runtime-minted token: nothing in the environment backs it.
        monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)

        token = _fake_shopify_token("shpat")
        _append_to_script_log(
            "postinstall",
            "shell",
            "shopify app env show",
            stdout=f"SHOPIFY_ACCESS_TOKEN={token}\n",
            status="ok",
            exit_code=0,
        )

        log_path = script_executors._get_scripts_log_path()
        contents = log_path.read_text(encoding="utf-8")
        assert token not in contents, (
            "Shopify access token persisted in cleartext in scripts.log; "
            f"found leaked credential in log file at {log_path}"
        )
    finally:
        wd.cancel()


# ---------------------------------------------------------------------------
# Finding r30-env-2: Stripe TEST-mode secret / restricted keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["sk", "rk"])
def test_round30_stripe_test_key_redacted_from_output(prefix):
    """A Stripe test-mode secret/restricted key must not survive redaction."""
    wd = _watchdog()
    try:
        key = _fake_stripe_test_key(prefix)
        stdout = f"Using account secret key {key}\n"
        redacted = _redact_secrets(stdout)
        assert key not in redacted, (
            f"Stripe {prefix}_test_ key leaked through _redact_secrets: "
            "cleartext secret key present in redaction output"
        )
    finally:
        wd.cancel()


def test_round30_stripe_test_key_redacted_in_scripts_log(tmp_path, monkeypatch):
    """End-to-end: the real log writer must not persist a Stripe test key."""
    wd = _watchdog()
    try:
        monkeypatch.setenv("APM_HOME", str(tmp_path))
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

        key = _fake_stripe_test_key("sk")
        _append_to_script_log(
            "postinstall",
            "shell",
            "stripe config --list",
            stdout=f"test_mode_api_key = {key}\n",
            status="ok",
            exit_code=0,
        )

        log_path = script_executors._get_scripts_log_path()
        contents = log_path.read_text(encoding="utf-8")
        assert key not in contents, (
            "Stripe test secret key persisted in cleartext in scripts.log; "
            f"found leaked credential in log file at {log_path}"
        )
    finally:
        wd.cancel()


# ---------------------------------------------------------------------------
# False-positive guard: benign look-alikes MUST survive any new masker arm.
# These pass on HEAD (no masker touches them) and MUST keep passing after the
# fix -- a masker that corrupts benign output is itself a bug.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "benign",
    [
        "risk_assessment_module",  # ends in 'sk_...' but no _test_/_live_ token
        "shipment_tracker_service",  # starts 'shp' but not shp**_+32hex
        "shop_at_home.example.com",  # 'shop_at' near 'shpat'
        "task_test_runner_config",  # 'sk_test' substring, no 16+ body
        "desk_test_id=42",  # 'sk_test_id' but short benign value
        "a1b2c3d4e5f60718293a4b5c6d7e8f90",  # bare 32-hex git sha, no shp prefix
        "https://shop.example.com/admin",  # shop host URL
        "sharepoint_config_path",  # 'shp' look-alike
        "PYTHONHASHSEED=12345",  # reproducible-build var must survive
    ],
)
def test_round30_benign_lookalikes_survive(benign):
    """Benign env values / paths / words must not be redacted by new arms."""
    wd = _watchdog()
    try:
        assert benign in _redact_secrets(benign), (
            f"benign look-alike was corrupted by redaction: {benign!r}"
        )
    finally:
        wd.cancel()
