"""Unit tests for ``apm_cli.integration.copilot_app_db``.

Exercises the I/O boundary in isolation against a temp SQLite file that
mirrors the live Copilot App schema (dumped from a real ``~/.copilot/data.db``
on a developer machine -- see fixture below).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apm_cli.integration import copilot_app_db as cdb

# ---------------------------------------------------------------------------
# Schema fixture -- mirrors live Copilot App ``data.db`` (user_version=13).
# ---------------------------------------------------------------------------

_WORKFLOWS_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS "workflows" (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT,
    reasoning_effort TEXT,
    project_id TEXT,
    interval TEXT NOT NULL CHECK (interval IN ('manual', 'hourly', 'daily', 'weekly')),
    schedule_hour INTEGER NOT NULL DEFAULT 9,
    schedule_day INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_run_at TEXT,
    next_run_at TEXT,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflows_enabled_next
    ON "workflows"(enabled, next_run_at);
"""


def _make_db(path: Path, user_version: int = 13) -> Path:
    """Build a fresh DB at *path* with the workflows schema + user_version."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_WORKFLOWS_SCHEMA)
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _make_db(tmp_path / "data.db")


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


class TestResolve:
    def test_env_override_present(self, tmp_path: Path, monkeypatch):
        db = _make_db(tmp_path / "custom.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))
        assert cdb.resolve_copilot_app_db_path() == db
        assert cdb.resolve_copilot_app_root() == db.parent

    def test_env_override_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(tmp_path / "nope.db"))
        assert cdb.resolve_copilot_app_db_path() is None
        assert cdb.resolve_copilot_app_root() is None

    def test_home_missing_returns_none(self, tmp_path: Path, monkeypatch):
        # Point HOME at an empty tmpdir; no env override.
        monkeypatch.delenv("APM_COPILOT_APP_DB", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert cdb.resolve_copilot_app_db_path() is None


# ---------------------------------------------------------------------------
# Version guard.
# ---------------------------------------------------------------------------


class TestVersionGuard:
    def test_accepts_user_version_13(self, db_path: Path):
        row = cdb.WorkflowRow(
            id=cdb.namespaced_id("o", "p", "n"),
            name="N",
            prompt="hi",
        )
        cdb.deploy_workflow(db_path, row)  # no raise

    def test_rejects_user_version_below_min(self, tmp_path: Path):
        db = _make_db(tmp_path / "old.db", user_version=12)
        row = cdb.WorkflowRow(id=cdb.namespaced_id("o", "p", "n"), name="N", prompt="x")
        with pytest.raises(cdb.CopilotAppDbSchemaError, match=r"older than supported"):
            cdb.deploy_workflow(db, row)

    @pytest.mark.parametrize("version", [16, 17, 50])
    def test_warns_but_continues_above_max(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        version: int,
    ):
        """Forward-compat: newer-than-tested schemas warn, do not raise.

        The Copilot App bumps ``user_version`` on additive changes that
        do not break APM's read/write surface; hard-failing made every
        App release a release window for APM.  See devx-ux verdict in
        the schema-warn PR.
        """
        cdb._reset_user_version_warning_state()
        db = _make_db(tmp_path / f"v{version}.db", user_version=version)
        row = cdb.WorkflowRow(id=cdb.namespaced_id("o", "p", "n"), name="N", prompt="x")
        cdb.deploy_workflow(db, row)  # no raise
        captured = capsys.readouterr()
        combined = " ".join((captured.out + captured.err).split())
        assert "[!]" in combined
        assert f"version {version}" in combined
        assert str(cdb._MAX_SUPPORTED_USER_VERSION) in combined
        assert "gh issue new" in combined

    def test_above_max_warning_deduped_per_version(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        """Multi-row install must warn once per version, not per row."""
        cdb._reset_user_version_warning_state()
        db = _make_db(tmp_path / "v20.db", user_version=20)
        for i in range(3):
            row = cdb.WorkflowRow(id=cdb.namespaced_id("o", "p", f"n{i}"), name=f"N{i}", prompt="x")
            cdb.deploy_workflow(db, row)
        captured = capsys.readouterr()
        combined = " ".join((captured.out + captured.err).split())
        # Dedup contract: a single warning line for v20 across N rows.
        assert combined.count("[!] Copilot App schema version 20") == 1
        assert 20 in cdb._warned_user_versions


# ---------------------------------------------------------------------------
# Deploy: INSERT + UPDATE semantics.
# ---------------------------------------------------------------------------


def _select_row(db_path: Path, wid: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM workflows WHERE id = ?", (wid,))
        r = cur.fetchone()
        assert r is not None
        return dict(r)
    finally:
        conn.close()


class TestDeploy:
    def test_insert_writes_full_row(self, db_path: Path):
        wid = cdb.namespaced_id("alice", "news", "daily")
        row = cdb.WorkflowRow(
            id=wid,
            name="Daily News",
            prompt="Summarise today's headlines",
            interval="daily",
            schedule_hour=9,
            schedule_day=1,
            enabled=0,
            mode="interactive",
            model="gpt-4o",
        )
        uri = cdb.deploy_workflow(db_path, row)
        assert uri == f"copilot-app-db://workflows/{wid}"
        stored = _select_row(db_path, wid)
        assert stored["name"] == "Daily News"
        assert stored["prompt"] == "Summarise today's headlines"
        assert stored["interval"] == "daily"
        assert stored["enabled"] == 0
        assert stored["mode"] == "interactive"
        assert stored["model"] == "gpt-4o"

    def test_insert_forces_enabled_zero_even_if_caller_passes_one(self, db_path: Path):
        """Defence in depth: writer ignores row.enabled on INSERT to block bootstrap."""
        wid = cdb.namespaced_id("evil", "pkg", "auto")
        row = cdb.WorkflowRow(
            id=wid,
            name="Hostile Auto-Run",
            prompt="anything",
            interval="daily",
            schedule_hour=9,
            schedule_day=1,
            enabled=1,
            mode="interactive",
            model="gpt-4o",
        )
        cdb.deploy_workflow(db_path, row)
        stored = _select_row(db_path, wid)
        assert stored["enabled"] == 0, "INSERT must force enabled=0 regardless of caller input"

    def test_update_preserves_enabled_when_only_name_changes(self, db_path: Path):
        """User's opt-in MUST survive a no-op metadata refresh."""
        wid = cdb.namespaced_id("alice", "news", "daily")
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V1", prompt="body", interval="manual"),
        )
        # Simulate the user enabling the row + the app recording a run.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE workflows SET enabled=1, last_run_at=?, next_run_at=? WHERE id=?",
                ("2025-01-01T00:00:00.000Z", "2025-01-02T00:00:00.000Z", wid),
            )
            conn.commit()
        finally:
            conn.close()
        # Re-deploy with ONLY the display name changed -- no execution-
        # affecting fields move.
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V1-renamed", prompt="body", interval="manual"),
        )
        stored = _select_row(db_path, wid)
        assert stored["name"] == "V1-renamed"
        assert stored["prompt"] == "body"
        assert stored["interval"] == "manual"
        assert stored["enabled"] == 1, "user opt-in must survive no-op refresh"
        assert stored["last_run_at"] == "2025-01-01T00:00:00.000Z"
        assert stored["next_run_at"] == "2025-01-02T00:00:00.000Z"

    def test_update_resets_enabled_when_prompt_body_changes(self, db_path: Path):
        """Content change revokes the user's prior opt-in (silent-update vector)."""
        wid = cdb.namespaced_id("alice", "news", "daily")
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V1", prompt="old", interval="manual"),
        )
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE workflows SET enabled=1, last_run_at=?, next_run_at=? WHERE id=?",
                ("2025-01-01T00:00:00.000Z", "2025-01-02T00:00:00.000Z", wid),
            )
            conn.commit()
        finally:
            conn.close()
        # Re-deploy with a new prompt body (the package was updated).
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V2", prompt="new", interval="hourly"),
        )
        stored = _select_row(db_path, wid)
        assert stored["name"] == "V2"
        assert stored["prompt"] == "new"
        assert stored["interval"] == "hourly"
        assert stored["enabled"] == 0, "content change must revoke prior opt-in"
        assert stored["next_run_at"] is None, "next_run_at must clear on content change"
        # last_run_at is history -- preserved either way.
        assert stored["last_run_at"] == "2025-01-01T00:00:00.000Z"

    def test_update_resets_enabled_when_schedule_changes(self, db_path: Path):
        """Schedule change is also an execution-affecting change."""
        wid = cdb.namespaced_id("alice", "news", "daily")
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V1", prompt="body", interval="daily", schedule_hour=9),
        )
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE workflows SET enabled=1 WHERE id=?",
                (wid,),
            )
            conn.commit()
        finally:
            conn.close()
        cdb.deploy_workflow(
            db_path,
            cdb.WorkflowRow(id=wid, name="V1", prompt="body", interval="daily", schedule_hour=17),
        )
        stored = _select_row(db_path, wid)
        assert stored["enabled"] == 0
        assert stored["schedule_hour"] == 17

    def test_rejects_invalid_interval(self, db_path: Path):
        wid = cdb.namespaced_id("o", "p", "n")
        with pytest.raises(ValueError, match=r"Invalid interval"):
            cdb.deploy_workflow(
                db_path,
                cdb.WorkflowRow(id=wid, name="N", prompt="x", interval="yearly"),
            )

    def test_rejects_invalid_mode(self, db_path: Path):
        wid = cdb.namespaced_id("o", "p", "n")
        with pytest.raises(ValueError, match=r"Invalid mode"):
            cdb.deploy_workflow(
                db_path,
                cdb.WorkflowRow(id=wid, name="N", prompt="x", mode="rogue"),
            )

    def test_rejects_autopilot_mode(self, db_path: Path):
        """autopilot is intentionally not accepted via the copilot-app target."""
        wid = cdb.namespaced_id("o", "p", "n")
        with pytest.raises(ValueError, match=r"autopilot"):
            cdb.deploy_workflow(
                db_path,
                cdb.WorkflowRow(id=wid, name="N", prompt="x", mode="autopilot"),
            )

    def test_rejects_non_apm_id(self, db_path: Path):
        with pytest.raises(ValueError, match=r"non-APM workflow id"):
            cdb.deploy_workflow(
                db_path,
                cdb.WorkflowRow(id="user-workflow", name="N", prompt="x"),
            )

    def test_missing_db_raises_missing_error(self, tmp_path: Path):
        wid = cdb.namespaced_id("o", "p", "n")
        with pytest.raises(cdb.CopilotAppDbMissingError, match=r"not found"):
            cdb.deploy_workflow(
                tmp_path / "nope.db",
                cdb.WorkflowRow(id=wid, name="N", prompt="x"),
            )


# ---------------------------------------------------------------------------
# Delete: namespace defence + idempotency.
# ---------------------------------------------------------------------------


class TestDelete:
    def test_removes_only_specified_apm_rows(self, db_path: Path):
        wid_a = cdb.namespaced_id("o", "p", "a")
        wid_b = cdb.namespaced_id("o", "p", "b")
        cdb.deploy_workflow(db_path, cdb.WorkflowRow(id=wid_a, name="A", prompt="x"))
        cdb.deploy_workflow(db_path, cdb.WorkflowRow(id=wid_b, name="B", prompt="y"))
        # User-created row (raw INSERT, no APM prefix).
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO workflows (id, name, prompt, interval) VALUES (?,?,?,?)",
                ("user-row", "Mine", "p", "manual"),
            )
            conn.commit()
        finally:
            conn.close()
        removed = cdb.delete_workflows(db_path, [wid_a])
        assert removed == 1
        # b + user-row survive.
        ids = cdb.list_managed_workflow_ids(db_path)
        assert ids == [wid_b]
        # user-row is still in the DB.
        conn = sqlite3.connect(str(db_path))
        try:
            present = conn.execute("SELECT 1 FROM workflows WHERE id = ?", ("user-row",)).fetchone()
            assert present is not None
        finally:
            conn.close()

    def test_refuses_non_apm_id(self, db_path: Path):
        with pytest.raises(ValueError, match=r"non-APM workflow id"):
            cdb.delete_workflows(db_path, ["user-row"])

    def test_missing_db_returns_zero(self, tmp_path: Path):
        # Idempotent uninstall.
        assert cdb.delete_workflows(tmp_path / "nope.db", []) == 0
        assert cdb.delete_workflows(tmp_path / "nope.db", [cdb.namespaced_id("a", "b", "c")]) == 0

    def test_empty_list_noop(self, db_path: Path):
        assert cdb.delete_workflows(db_path, []) == 0


# ---------------------------------------------------------------------------
# list_managed_workflow_ids.
# ---------------------------------------------------------------------------


class TestList:
    def test_filters_to_apm_namespace(self, db_path: Path):
        wid = cdb.namespaced_id("o", "p", "n")
        cdb.deploy_workflow(db_path, cdb.WorkflowRow(id=wid, name="N", prompt="x"))
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO workflows (id, name, prompt, interval) VALUES (?,?,?,?)",
                ("hand-rolled", "H", "p", "manual"),
            )
            conn.commit()
        finally:
            conn.close()
        assert cdb.list_managed_workflow_ids(db_path) == [wid]

    def test_missing_db_returns_empty(self, tmp_path: Path):
        assert cdb.list_managed_workflow_ids(tmp_path / "nope.db") == []
