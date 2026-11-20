"""Unit tests for ``apm_cli.integration.copilot_app_project``.

Covers:

* ``derive_repo_context`` -- no-repo / no-remote / GitHub-remote /
  detached-HEAD branches.
* ``derive_project_recipe`` -- pure projection.
* ``resolve_or_register_project_sqlite`` -- MISS (INSERT),
  HIT (SELECT-only), race-collision recovery, missing-DB error,
  malformed schema rejection.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from apm_cli.integration import copilot_app_db as cdb
from apm_cli.integration import copilot_app_project as cap

# ---------------------------------------------------------------------------
# Schema fixture -- includes the workflows table (required by user_version
# guard) plus the projects table (subject under test).
# ---------------------------------------------------------------------------

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
    mode TEXT
);
"""


def _make_db(path: Path, user_version: int = 13) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _make_db(tmp_path / "data.db")


def _git_init(repo_root: Path, *, remote: str | None = None) -> None:
    """Minimal local git init so derive_repo_context has something to bite."""
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=repo_root,
        check=True,
    )
    if remote is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=repo_root,
            check=True,
        )


# ---------------------------------------------------------------------------
# Pure derive_* tests.
# ---------------------------------------------------------------------------


class TestDeriveRepoContext:
    def test_no_repo_returns_none(self, tmp_path: Path) -> None:
        assert cap.derive_repo_context(tmp_path) is None

    def test_plain_repo_no_remote(self, tmp_path: Path) -> None:
        repo = tmp_path / "my-repo"
        _git_init(repo)
        ctx = cap.derive_repo_context(repo)
        assert ctx is not None
        assert ctx.repo_root == repo.resolve()
        assert ctx.repo_name == "my-repo"
        assert ctx.github_owner is None
        assert ctx.github_repo is None
        # default_branch defaults to "main" when detection succeeds OR
        # when it fails -- either way the field is populated.
        assert ctx.default_branch

    def test_github_https_remote(self, tmp_path: Path) -> None:
        repo = tmp_path / "demo"
        _git_init(repo, remote="https://github.com/octo/widgets.git")
        ctx = cap.derive_repo_context(repo)
        assert ctx is not None
        assert ctx.github_owner == "octo"
        assert ctx.github_repo == "widgets"
        # repo_name prefers the remote-derived name over folder name.
        assert ctx.repo_name == "widgets"

    def test_github_ssh_remote(self, tmp_path: Path) -> None:
        repo = tmp_path / "demo2"
        _git_init(repo, remote="git@github.com:octo/widgets.git")
        ctx = cap.derive_repo_context(repo)
        assert ctx is not None
        assert ctx.github_owner == "octo"
        assert ctx.github_repo == "widgets"

    def test_non_github_remote_yields_none_owner(self, tmp_path: Path) -> None:
        repo = tmp_path / "demo3"
        _git_init(repo, remote="https://gitlab.com/octo/widgets.git")
        ctx = cap.derive_repo_context(repo)
        assert ctx is not None
        assert ctx.github_owner is None
        assert ctx.github_repo is None
        # repo_name falls back to folder name.
        assert ctx.repo_name == "demo3"

    def test_finds_repo_root_from_subdir(self, tmp_path: Path) -> None:
        repo = tmp_path / "root"
        _git_init(repo)
        subdir = repo / "a" / "b"
        subdir.mkdir(parents=True)
        ctx = cap.derive_repo_context(subdir)
        assert ctx is not None
        assert ctx.repo_root == repo.resolve()


class TestDeriveProjectRecipe:
    def test_pure_projection(self) -> None:
        ctx = cap.RepoContext(
            repo_root=Path("/tmp/x"),
            repo_name="x",
            github_owner="o",
            github_repo="x",
            default_branch="main",
        )
        r = cap.derive_project_recipe(ctx)
        assert r.name == "x"
        # ``main_repo_path`` is the OS-native string form of the repo
        # root; on POSIX this is ``/tmp/x``, on Windows ``\tmp\x``.
        assert r.main_repo_path == str(Path("/tmp/x"))
        assert r.default_branch == "main"
        assert r.github_owner == "o"
        assert r.github_repo == "x"
        # Dashed UUID4 -- 36 chars including hyphens.
        assert len(r.id) == 36
        assert r.id.count("-") == 4


# ---------------------------------------------------------------------------
# resolve_or_register_project_sqlite tests.
# ---------------------------------------------------------------------------


def _make_ctx(repo: Path, *, repo_name: str = "demo") -> cap.RepoContext:
    return cap.RepoContext(
        repo_root=repo.resolve(),
        repo_name=repo_name,
        github_owner="octo",
        github_repo=repo_name,
        default_branch="main",
    )


class TestResolverMiss:
    def test_insert_creates_row(self, db_path: Path, tmp_path: Path) -> None:
        repo = tmp_path / "miss-repo"
        repo.mkdir()
        ctx = _make_ctx(repo)
        resolved = cap.resolve_or_register_project_sqlite(db_path, ctx)
        assert resolved.was_created is True
        assert resolved.project_id  # non-empty
        assert resolved.main_repo_path == str(repo.resolve())
        # Verify row landed with the expected shape.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?",
                (resolved.project_id,),
            ).fetchone()
            assert row is not None
            assert row["name"] == "demo"
            assert row["main_repo_path"] == str(repo.resolve())
            assert row["container_kind"] == "repository"
            assert row["default_branch"] == "main"
            assert row["github_owner"] == "octo"
        finally:
            conn.close()


class TestResolverHit:
    def test_returns_existing_id_without_insert(self, db_path: Path, tmp_path: Path) -> None:
        repo = tmp_path / "hit-repo"
        repo.mkdir()
        ctx = _make_ctx(repo)
        first = cap.resolve_or_register_project_sqlite(db_path, ctx)
        second = cap.resolve_or_register_project_sqlite(db_path, ctx)
        assert first.was_created is True
        assert second.was_created is False
        assert first.project_id == second.project_id

    def test_external_row_is_reused(self, db_path: Path, tmp_path: Path) -> None:
        """An existing App-native row (different id) is reused, not duplicated."""
        repo = tmp_path / "external-repo"
        repo.mkdir()
        external_id = "ffffffff-0000-4000-8000-000000000001"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO projects (id, name, container_kind, main_repo_path) "
                "VALUES (?, ?, 'repository', ?)",
                (external_id, "demo", str(repo.resolve())),
            )
            conn.commit()
        finally:
            conn.close()
        resolved = cap.resolve_or_register_project_sqlite(db_path, _make_ctx(repo))
        assert resolved.was_created is False
        assert resolved.project_id == external_id


class TestResolverErrors:
    def test_missing_db_raises_missing_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "norepo"
        repo.mkdir()
        with pytest.raises(cdb.CopilotAppDbMissingError, match=r"not found"):
            cap.resolve_or_register_project_sqlite(tmp_path / "absent.db", _make_ctx(repo))

    def test_schema_too_new_warns_and_continues(self, tmp_path: Path, capsys) -> None:
        """A user_version above the tested max warns but does NOT raise.

        See commit 05ea7780: the Copilot App ships fast and bumps
        user_version on additive changes that do not break APM's
        read/write surface, so we warn-and-continue rather than gate
        every install on an APM release.
        """
        cdb._reset_user_version_warning_state()
        bad = _make_db(tmp_path / "newer.db", user_version=999)
        repo = tmp_path / "r"
        repo.mkdir()
        resolved = cap.resolve_or_register_project_sqlite(bad, _make_ctx(repo))
        assert resolved.project_id  # resolver still succeeds
        captured = capsys.readouterr()
        # Warning surfaces the exact version delta.
        assert "999" in (captured.out + captured.err)


class TestRaceCollisionRecovery:
    def test_race_collision_resolves_to_winning_id(
        self, db_path: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """Simulate concurrent INSERT mid-transaction.

        Captures the connection the resolver opens via ``cdb._connect``,
        then monkeypatches ``derive_project_recipe`` (called between SELECT
        and INSERT) to insert a colliding row on the SAME connection so
        the resolver's INSERT trips ``sqlite3.IntegrityError`` and falls
        into the race-recovery SELECT branch.
        """
        repo = tmp_path / "race-repo"
        repo.mkdir()
        winning_id = "11111111-2222-4333-8444-555555555555"

        captured: dict[str, sqlite3.Connection] = {}
        real_connect = cdb._connect

        def capturing_connect(path):
            conn = real_connect(path)
            captured["c"] = conn
            return conn

        monkeypatch.setattr(cdb, "_connect", capturing_connect)

        real_derive = cap.derive_project_recipe

        def colliding_derive(ctx):
            recipe = real_derive(ctx)
            conn = captured["c"]
            conn.execute(
                "INSERT INTO projects (id, name, container_kind, main_repo_path) "
                "VALUES (?, ?, 'repository', ?)",
                (winning_id, ctx.repo_name, str(ctx.repo_root)),
            )
            return recipe

        monkeypatch.setattr(cap, "derive_project_recipe", colliding_derive)
        resolved = cap.resolve_or_register_project_sqlite(db_path, _make_ctx(repo))
        # Race recovery returns the WINNING (external) id, not our recipe id.
        assert resolved.project_id == winning_id
        assert resolved.was_created is False

    def test_race_collision_no_recovery_row_raises(
        self, db_path: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """IntegrityError on a different constraint surfaces as a DB error.

        Patches ``derive_project_recipe`` to return a recipe whose ``id``
        collides with a pre-existing row that has a DIFFERENT
        ``main_repo_path``, so the race-recovery SELECT finds nothing
        for our path. The resolver MUST raise rather than silently
        invent a project id.
        """
        repo = tmp_path / "no-recover-repo"
        repo.mkdir()
        clashing_id = "22222222-3333-4444-8555-666666666666"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO projects (id, name, container_kind, main_repo_path) "
                "VALUES (?, ?, 'repository', ?)",
                (clashing_id, "other", str(tmp_path / "other-path")),
            )
            conn.commit()
        finally:
            conn.close()

        def clashing_derive(ctx):
            base = cap.ProjectRecipe(
                id=clashing_id,
                name=ctx.repo_name,
                main_repo_path=str(ctx.repo_root),
                default_branch="main",
                github_owner=None,
                github_repo=None,
            )
            return base

        monkeypatch.setattr(cap, "derive_project_recipe", clashing_derive)
        with pytest.raises(cdb.CopilotAppDbError, match=r"Race recovery"):
            cap.resolve_or_register_project_sqlite(db_path, _make_ctx(repo))
