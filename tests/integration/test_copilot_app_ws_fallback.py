"""Integration test for the hybrid WS-then-SQLite project-registration
dispatch in ``PromptIntegrator._integrate_prompts_for_copilot_app``.

The PR that introduced the hybrid path claims a single deterministic
contract: when the App is running (``ws_available() is True``) we try
``WsClient.create_project_from_path`` first; on any ``WsError`` we
fall through to ``resolve_or_register_project_sqlite``; in BOTH
branches the workflow rows are then written via direct SQLite stamped
with the resolved ``project_id``.

The unit suite covers each branch in isolation but not the mid-deploy
``WsError`` fallback that the PR's own dispatch diagram highlights.
Without this test, a future refactor could silently bypass the SQLite
path on WsError (e.g. by raising out of the integrator instead of
catching) and no existing assertion would notice. This test is the
regression trap for that specific drift.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from apm_cli.integration import copilot_app_ws as ws_mod
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS "projects" (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    container_kind TEXT NOT NULL,
    main_repo_path TEXT UNIQUE,
    default_branch TEXT,
    github_owner TEXT,
    github_repo TEXT,
    github_account_id TEXT,
    tab_order INTEGER,
    issue_prompt TEXT,
    pull_request_prompt TEXT,
    auto_open_in_browser INTEGER NOT NULL DEFAULT 1,
    auto_approve INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS "workflows" (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval TEXT NOT NULL CHECK (interval IN ('manual', 'hourly', 'daily', 'weekly')),
    schedule_hour INTEGER NOT NULL DEFAULT 9,
    schedule_day INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    project_id TEXT,
    mode TEXT,
    model TEXT,
    reasoning_effort TEXT
);
"""


WORKFLOW_PROMPT = """---
name: Daily Digest
interval: daily
schedule_hour: 9
mode: interactive
---
Summarise yesterday's commits.
"""


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"],
    ):
        subprocess.run(args, cwd=repo, check=True)


def _make_pkg(repo: Path) -> SimpleNamespace:
    pkg = repo / "node_modules" / "@acme" / "demo"
    prompts = pkg / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "daily-digest.prompt.md").write_text(WORKFLOW_PROMPT)
    return SimpleNamespace(
        install_path=pkg,
        package=SimpleNamespace(
            name="demo",
            source="github:acme/demo",
            author=None,
        ),
    )


class _Diagnostics:
    def __init__(self) -> None:
        self.warns: list[dict] = []
        self.infos: list[dict] = []

    def warn(self, **kwargs) -> None:
        self.warns.append(kwargs)

    def info(self, **kwargs) -> None:
        self.infos.append(kwargs)


def test_ws_error_mid_deploy_falls_through_to_sqlite_with_project_scoping(
    tmp_path, monkeypatch
) -> None:
    """WS available + create_project_from_path raises WsError ->

    1. The integrator does NOT propagate the WsError.
    2. SQLite ``resolve_or_register_project_sqlite`` registers the
       project at ``repo_root`` and assigns a ``project_id``.
    3. Each workflow row is stamped with that ``project_id``.
    4. A user-visible warn explains the WS path was unreachable.
    5. The one-time restart hint fires because the project was new.
    """
    repo = tmp_path / "repo"
    _git_init(repo)

    db_path = tmp_path / "data.db"
    _init_db(db_path)
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(db_path))

    pkg = _make_pkg(repo)

    # Force ws_available() True regardless of the host's real run-dir.
    monkeypatch.setattr(ws_mod, "ws_available", lambda: True)

    # Make the WS client's create_project_from_path raise a generic
    # WsProtocolError mid-deploy -- the exact failure mode the PR's
    # hybrid-dispatch diagram highlights ("WsError -> SQLite").
    def boom(self, path):
        raise ws_mod.WsProtocolError("simulated upstream protocol drift")

    monkeypatch.setattr(ws_mod.WsClient, "create_project_from_path", boom)
    # ``__enter__`` calls ``_connect`` which tries to read creds; bypass
    # that whole machinery -- we just need the method-on-an-instance
    # to raise WsProtocolError to drive the fallback branch.
    monkeypatch.setattr(ws_mod.WsClient, "_connect", lambda self: None)
    monkeypatch.setattr(ws_mod.WsClient, "_drain", lambda self, timeout_s: [])

    target = KNOWN_TARGETS["copilot-app"]
    diags = _Diagnostics()

    result = PromptIntegrator().integrate_prompts_for_target(
        target,
        pkg,
        project_root=repo,
        diagnostics=diags,
    )

    # Workflow row landed despite the WS branch failing.
    assert result.files_integrated == 1
    assert result.files_skipped == 0

    # SQLite has a projects row scoped to the repo and a workflows row
    # carrying that project_id.
    conn = sqlite3.connect(str(db_path))
    try:
        projects = conn.execute("SELECT id, name, main_repo_path FROM projects").fetchall()
        assert len(projects) == 1
        project_id, _project_name, main_repo_path = projects[0]
        assert project_id  # non-empty UUID
        assert main_repo_path == str(repo.resolve())

        workflows = conn.execute("SELECT id, name, project_id FROM workflows").fetchall()
        assert len(workflows) == 1
        wf_id, wf_name, wf_project_id = workflows[0]
        assert wf_project_id == project_id, (
            "Workflow row must be scoped to the SQLite-registered project"
        )
        # Namespaced id discipline preserved across the fallback branch.
        assert wf_id.startswith("apm--")
        # Display name suffixed with (repo_name) for sidebar disambiguation.
        assert "(widgets)" in wf_name or "(repo)" in wf_name
    finally:
        conn.close()

    # User-visible warn explains the WS-side failure in plain wording
    # (no internal jargon like "WS-IPC" / "handshake" / "broadcast").
    ws_warns = [w for w in diags.warns if "Copilot App" in w.get("message", "")]
    assert ws_warns, "expected a user-facing warn about the WS failure"
    msg = ws_warns[0]["message"]
    assert "Could not reach the running Copilot App" in msg
    for jargon in ("WS-IPC", "handshake failed", "live IPC unavailable"):
        assert jargon not in msg, f"User-facing warn must not leak {jargon!r}; got {msg!r}"

    # The one-time restart hint fires because the SQLite branch
    # registered a brand-new project row.
    restart_infos = [
        i for i in diags.infos if "Restart the Copilot App once" in i.get("message", "")
    ]
    assert len(restart_infos) == 1, (
        "Expected exactly one restart-once hint after the project was newly created"
    )


def test_ws_app_not_running_falls_through_silently(tmp_path, monkeypatch) -> None:
    """WsAppNotRunning is the App-closed signal -- silent fallback, no warn."""
    repo = tmp_path / "repo"
    _git_init(repo)

    db_path = tmp_path / "data.db"
    _init_db(db_path)
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(db_path))

    pkg = _make_pkg(repo)

    monkeypatch.setattr(ws_mod, "ws_available", lambda: True)

    def app_closed(self, path):
        raise ws_mod.WsAppNotRunning("port file went away mid-handshake")

    monkeypatch.setattr(ws_mod.WsClient, "create_project_from_path", app_closed)
    monkeypatch.setattr(ws_mod.WsClient, "_connect", lambda self: None)
    monkeypatch.setattr(ws_mod.WsClient, "_drain", lambda self, timeout_s: [])

    target = KNOWN_TARGETS["copilot-app"]
    diags = _Diagnostics()

    result = PromptIntegrator().integrate_prompts_for_target(
        target,
        pkg,
        project_root=repo,
        diagnostics=diags,
    )

    assert result.files_integrated == 1

    # WsAppNotRunning is silent -- closed-App is the normal off-state.
    ws_warns = [
        w for w in diags.warns if "Could not reach the running Copilot App" in w.get("message", "")
    ]
    assert ws_warns == [], "WsAppNotRunning must NOT surface a user-visible warn"


def test_ws_auth_error_falls_through_silently(tmp_path, monkeypatch) -> None:
    """WsAuthError (stale token) is silent fallback -- the restart hint
    already covers the user-visible signal; nagging on every install
    is a UX regression flagged by the panel."""
    repo = tmp_path / "repo"
    _git_init(repo)

    db_path = tmp_path / "data.db"
    _init_db(db_path)
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(db_path))

    pkg = _make_pkg(repo)

    monkeypatch.setattr(ws_mod, "ws_available", lambda: True)

    def stale_token(self, path):
        raise ws_mod.WsAuthError("WS auth rejected: token=<redacted>")

    monkeypatch.setattr(ws_mod.WsClient, "create_project_from_path", stale_token)
    monkeypatch.setattr(ws_mod.WsClient, "_connect", lambda self: None)
    monkeypatch.setattr(ws_mod.WsClient, "_drain", lambda self, timeout_s: [])

    target = KNOWN_TARGETS["copilot-app"]
    diags = _Diagnostics()

    PromptIntegrator().integrate_prompts_for_target(
        target,
        pkg,
        project_root=repo,
        diagnostics=diags,
    )

    ws_warns = [
        w for w in diags.warns if "Could not reach the running Copilot App" in w.get("message", "")
    ]
    assert ws_warns == [], (
        "WsAuthError (stale token) must NOT surface a user-visible warn; "
        "the restart-once hint already covers the signal"
    )
