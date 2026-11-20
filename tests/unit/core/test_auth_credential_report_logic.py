"""Helper credentials are reported back to Git per the git-credential contract (#3765) (pure logic cases)."""

from __future__ import annotations

import sys

import pytest

from apm_cli.core import auth_chain

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX helper launcher"),
]

HOST = "dev.azure.com"
HELPER_PATH = "contoso/platform/_git/agent-skills"


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# no report for ambiguous outcomes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# env PAT, az bearer, and non-ADO credentials are never reported
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# host scoping and fail-safe behavior
# ---------------------------------------------------------------------------


def test_non_ascii_secret_is_compared_without_error():
    ledger = auth_chain.HelperReportLedger()
    key = (HOST, None, HELPER_PATH)
    assert ledger.decide(key, "approve", "p\u00e4ss-\u00fcber")
    assert not ledger.decide((HOST, None, "other"), "reject", "p\u00e4ss-\u00fcber")
    assert ledger.decide((HOST, None, "other"), "reject", "another-secret")
