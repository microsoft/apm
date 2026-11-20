"""Unit tests for ``apm_cli.integration.copilot_app_db`` (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.integration import copilot_app_db as cdb

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Schema fixture -- mirrors live Copilot App ``data.db`` (user_version=13).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Namespacing / URI helpers (pure functions).
# ---------------------------------------------------------------------------


class TestNamespacedId:
    def test_basic_format(self):
        assert cdb.namespaced_id("alice", "my-pkg", "daily-news") == (
            "apm--alice--my-pkg--daily-news"
        )

    def test_slugifies_unsafe_chars(self):
        assert cdb.namespaced_id("Al!ce", "pkg name", "p/r:o m_pt") == (
            "apm--al-ce--pkg-name--p-r-o-m_pt"
        )

    def test_empty_segments_become_unknown(self):
        # Empty owner gets replaced with "unknown" (defensive default).
        wid = cdb.namespaced_id("", "pkg", "p")
        assert wid.startswith("apm--unknown--")

    def test_is_apm_managed_id(self):
        assert cdb.is_apm_managed_id("apm--a--b--c")
        assert not cdb.is_apm_managed_id("my-workflow")
        assert not cdb.is_apm_managed_id("APM--a--b--c")  # case-sensitive


class TestLockfileUri:
    def test_roundtrip(self):
        wid = "apm--owner--pkg--prompt"
        uri = cdb.to_lockfile_uri(wid)
        assert uri == "copilot-app-db://workflows/apm--owner--pkg--prompt"
        assert cdb.from_lockfile_uri(uri) == wid

    def test_rejects_non_apm_id_on_encode(self):
        with pytest.raises(ValueError, match=r"non-APM workflow id"):
            cdb.to_lockfile_uri("user-workflow")

    def test_rejects_non_apm_id_on_decode(self):
        with pytest.raises(ValueError, match=r"non-APM workflow id"):
            cdb.from_lockfile_uri("copilot-app-db://workflows/user-workflow")

    def test_rejects_wrong_scheme(self):
        with pytest.raises(ValueError, match=r"Not a copilot-app lockfile URI"):
            cdb.from_lockfile_uri("cowork://skills/foo")

    def test_is_copilot_app_uri(self):
        assert cdb.is_copilot_app_uri("copilot-app-db://workflows/apm--a--b--c")
        assert not cdb.is_copilot_app_uri("cowork://skills/x")
        assert not cdb.is_copilot_app_uri(".github/skills/x")


# ---------------------------------------------------------------------------
# Resolver -- env override + presence-based discovery.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Version guard.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deploy: INSERT + UPDATE semantics.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Delete: namespace defence + idempotency.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_managed_workflow_ids.
# ---------------------------------------------------------------------------
