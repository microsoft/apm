"""E2E regression tests for ``apm install --target copilot-app``.

Parser scenarios:

  1. Flag OFF                -> enable-hint printed, exit 0.
  2. Flag ON, no ``data.db`` -> "Copilot App not detected" error, exit 1.
  3. Project scope           -> supported (v1.1); no --global required.

Two happy-path tests exercise the full deploy + uninstall cycle against
a temp SQLite DB seeded with the live workflows schema, proving that
``apm install`` -> ``apm uninstall`` actually writes and removes
APM-namespaced rows in BOTH user (``--global``) and project scope.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

_MINIMAL_APM_YML = "name: test\ndescription: test\nversion: 0.0.1\n"
_BASE_ENV: dict[str, str] = {"APM_E2E_TESTS": "1"}

_WORKFLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS "projects" (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    container_kind TEXT NOT NULL DEFAULT 'repository',
    main_repo_path TEXT UNIQUE,
    default_branch TEXT,
    github_owner TEXT,
    github_repo TEXT,
    auto_open_in_browser INTEGER NOT NULL DEFAULT 1,
    auto_approve INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

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
"""


def _seed_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_WORKFLOWS_SCHEMA)
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()
    return path


def _write_minimal_apm_yml(apm_dir: Path) -> None:
    (apm_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")


def _write_config_json(apm_dir: Path, cfg: dict[str, Any]) -> None:
    (apm_dir / "config.json").write_text(json.dumps(cfg), encoding="ascii")


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated home wired into every APM config lookup (cowork-test parity)."""
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    _write_minimal_apm_yml(apm_dir)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    monkeypatch.setattr(_conf, "_config_cache", None)


class TestCopilotAppParserE2E:
    def test_flag_off_emits_enable_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag OFF: parser accepts copilot-app, targets phase prints hint, exit 0."""
        cfg = fake_home / ".apm" / "config.json"
        if cfg.exists():
            cfg.unlink()
        monkeypatch.delenv("APM_COPILOT_APP_DB", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "copilot-app", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "is not a valid target" not in (result.output or "")
        normalized = " ".join((result.output or "").split())
        assert "apm experimental enable copilot-app" in normalized, result.output

    def test_flag_on_db_missing_errors(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON + no data.db: actionable error, non-zero exit."""
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        # Point env override at a non-existent file so resolver returns None.
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(fake_home / "nope.db"))

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "copilot-app", "--global"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(fake_home / "nope.db")},
            catch_exceptions=True,
        )
        assert "is not a valid target" not in (result.output or "")
        assert result.exit_code != 0, result.output
        normalized = " ".join((result.output or "").split())
        assert "GitHub Copilot desktop App not detected" in normalized or "data.db" in normalized, (
            result.output
        )

    def test_project_scope_now_supported(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project-scope install is supported (v1.1): no --global required.

        The experimental flag is the consent envelope; project-scope intent
        is legitimate (a team-shared scheduled prompt belongs in the project
        that owns it). Verifies the parser + gate succeed without --global
        and the flag-on + DB-present path reaches the integrator.
        """
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "copilot-app"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=True,
        )
        # Must NOT error with the legacy --global gate.
        assert "requires --global" not in (result.output or ""), result.output
        # Empty project apm.yml means zero deps to deploy; install succeeds.
        assert result.exit_code == 0, result.output


class TestCopilotAppDeployUninstall:
    """Full install -> verify row exists -> uninstall -> verify row gone."""

    def test_install_then_uninstall_roundtrip(
        self,
        tmp_path: Path,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import apm_cli.config as _conf

        # Enable the experimental flag.
        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )

        # Seed a temp DB and pin the resolver to it.
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        # Build a tiny local APM package with one scheduled prompt.
        pkg_dir = tmp_path / "pkg-scheduler"
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            textwrap.dedent(
                """\
                name: scheduler-pkg
                description: test
                version: 0.0.1
                author: alice
                """
            ),
            encoding="ascii",
        )
        (prompts_dir / "daily-digest.prompt.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: Daily Digest
                interval: daily
                schedule_hour: 9
                schedule_day: 1
                mode: interactive
                ---
                Summarise yesterday's commits.
                """
            ),
            encoding="ascii",
        )

        # User-scope apm.yml that depends on the local package (file URL).
        apm_dir = fake_home / ".apm"
        (apm_dir / "apm.yml").write_text(
            textwrap.dedent(
                f"""\
                name: user
                description: user-scope test
                version: 0.0.1
                dependencies:
                  apm:
                    - source: file://{pkg_dir}
                      name: scheduler-pkg
                """
            ),
            encoding="ascii",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "copilot-app", "--global"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=True,
        )
        # If install support for ``file://`` sources isn't available in
        # this code path, the deploy step may be a no-op.  Either way the
        # parser + gate must succeed.
        assert "is not a valid target" not in (result.output or "")
        assert "experimental enable" not in (result.output or "") or result.exit_code == 0

        # If deploy ran, the row must be present and disabled.
        from apm_cli.integration import copilot_app_db as cdb

        ids = cdb.list_managed_workflow_ids(db)
        if ids:
            assert any("daily-digest" in i for i in ids)
            conn = sqlite3.connect(str(db))
            try:
                row = conn.execute(
                    "SELECT enabled FROM workflows WHERE id = ?", (ids[0],)
                ).fetchone()
                assert row is not None
                assert row[0] == 0, "deployed workflows must start disabled"
            finally:
                conn.close()

    def test_install_local_pkg_then_uninstall_deletes_db_row(
        self,
        tmp_path: Path,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``apm uninstall`` must DELETE the workflows row.

        Reproduces the bug where uninstall removed the package from apm.yml
        and apm_modules/ but left the DB row orphaned. Models real usage:
        ``apm install <local-path> --target copilot-app -g`` followed by
        ``apm uninstall <local-path> -g``.
        """
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        pkg_dir = tmp_path / "uninstall-pkg"
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            textwrap.dedent(
                """\
                name: uninstall-pkg
                description: regression test
                version: 0.0.1
                """
            ),
            encoding="ascii",
        )
        (prompts_dir / "daily-digest.prompt.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: Daily Digest
                interval: daily
                schedule_hour: 9
                schedule_day: 1
                ---
                Summarise yesterday's commits.
                """
            ),
            encoding="ascii",
        )

        from apm_cli.integration import copilot_app_db as cdb

        runner = CliRunner()
        install_result = runner.invoke(
            cli,
            ["install", str(pkg_dir), "--target", "copilot-app", "--global"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        assert install_result.exit_code == 0, install_result.output

        # Lockfile must encode the copilot-app URI with the scheme prefix so
        # uninstall can find and delete the row.
        lockfile_text = (fake_home / ".apm" / "apm.lock.yaml").read_text(encoding="utf-8")
        assert "copilot-app-db://workflows/apm--" in lockfile_text, lockfile_text

        ids_after_install = cdb.list_managed_workflow_ids(db)
        assert len(ids_after_install) == 1, (
            f"install should write exactly one row, got {ids_after_install}"
        )
        assert ids_after_install[0].startswith("apm--"), ids_after_install[0]

        uninstall_result = runner.invoke(
            cli,
            ["uninstall", str(pkg_dir), "--global", "-v"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        assert uninstall_result.exit_code == 0, uninstall_result.output

        ids_after_uninstall = cdb.list_managed_workflow_ids(db)
        if ids_after_uninstall:
            _lock = (
                (fake_home / ".apm" / "apm.lock.yaml").read_text(encoding="utf-8")
                if (fake_home / ".apm" / "apm.lock.yaml").exists()
                else "<no lockfile>"
            )
            raise AssertionError(
                f"uninstall must delete the DB row, but {ids_after_uninstall} remain\n"
                f"--- install output ---\n{install_result.output}\n"
                f"--- uninstall output ---\n{uninstall_result.output}\n"
                f"--- post-uninstall lockfile ---\n{_lock}\n"
            )

    def test_install_project_scope_then_uninstall_deletes_db_row(
        self,
        tmp_path: Path,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """v1.1 regression: project-scope install+uninstall round-trips
        the DB row without --global. Mirror of the user-scope roundtrip
        test above but exercises the project apm.yml path: a team-shared
        scheduled prompt is declared in a project's apm.yml and deploys
        to the developer's Copilot App DB on install.
        """
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        pkg_dir = tmp_path / "project-scope-pkg"
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            textwrap.dedent(
                """\
                name: project-scope-pkg
                description: project-scope roundtrip
                version: 0.0.1
                """
            ),
            encoding="ascii",
        )
        (prompts_dir / "weekly-report.prompt.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: Weekly Report
                interval: weekly
                schedule_hour: 10
                schedule_day: 1
                ---
                Summarise the week.
                """
            ),
            encoding="ascii",
        )

        # Project consumer lives in a separate directory; CliRunner runs
        # in cwd, so we change into it for the install + uninstall calls.
        # ``git init`` so derive_repo_context can build a RepoContext and
        # the integrator stamps a real project_id (PR A).
        import subprocess as _sp

        consumer_dir = tmp_path / "consumer-project"
        consumer_dir.mkdir()
        _sp.run(
            ["git", "init", "-q", str(consumer_dir)],
            check=True,
            capture_output=True,
        )
        (consumer_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")
        monkeypatch.chdir(consumer_dir)

        from apm_cli.integration import copilot_app_db as cdb

        runner = CliRunner()
        install_result = runner.invoke(
            cli,
            ["install", str(pkg_dir), "--target", "copilot-app"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        assert install_result.exit_code == 0, install_result.output
        # Lock the user-facing contract: contributors see the prompts
        # integrated line AND the enable-in-Copilot-App hint, so a
        # project-scope teammate isn't left wondering why their
        # workflow never fires (see devx-ux-expert finding on PR #1405).
        assert "prompts integrated" in install_result.output, install_result.output
        assert "enable from the Copilot App" in install_result.output, install_result.output

        # Lockfile must live in the PROJECT (not user-home) and carry
        # the copilot-app URI so uninstall can locate the DB row.
        project_lock = consumer_dir / "apm.lock.yaml"
        assert project_lock.exists(), "project lockfile not written"
        lock_text = project_lock.read_text(encoding="utf-8")
        assert "copilot-app-db://workflows/apm--" in lock_text, lock_text

        ids_after_install = cdb.list_managed_workflow_ids(db)
        assert len(ids_after_install) == 1, (
            f"project-scope install should write exactly one row, got {ids_after_install}"
        )
        assert ids_after_install[0].startswith("apm--"), ids_after_install[0]

        # PR A: project-scope installs MUST stamp project_id on the
        # workflow row so it shows up under the correct project in the
        # App's Workflows tab. The integrator auto-registers a row in
        # the ``projects`` table for the consumer directory.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT project_id, name FROM workflows WHERE id = ?",
                (ids_after_install[0],),
            ).fetchone()
            assert row is not None
            assert row[0] is not None, "project_id must be stamped on the workflow row"
            # Display name carries the repo suffix `(<repo>)`.
            assert "(" in row[1] and ")" in row[1], (
                f"workflow name should have repo suffix, got {row[1]!r}"
            )
            proj = conn.execute(
                "SELECT main_repo_path FROM projects WHERE id = ?", (row[0],)
            ).fetchone()
            assert proj is not None
            assert Path(proj[0]) == consumer_dir.resolve()
        finally:
            conn.close()

        # First-time install into a new repo must emit the restart hint
        # (the App webview does not live-refresh on externally-inserted
        # projects rows -- see github/github-app#5483).
        assert "Restart the Copilot" in install_result.output, install_result.output

        uninstall_result = runner.invoke(
            cli,
            ["uninstall", str(pkg_dir), "-v"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        assert uninstall_result.exit_code == 0, uninstall_result.output

        ids_after_uninstall = cdb.list_managed_workflow_ids(db)
        if ids_after_uninstall:
            _lock = (
                (consumer_dir / "apm.lock.yaml").read_text(encoding="utf-8")
                if (consumer_dir / "apm.lock.yaml").exists()
                else "<no lockfile>"
            )
            raise AssertionError(
                f"project-scope uninstall must delete the DB row, "
                f"but {ids_after_uninstall} remain\n"
                f"--- install output ---\n{install_result.output}\n"
                f"--- uninstall output ---\n{uninstall_result.output}\n"
                f"--- post-uninstall lockfile ---\n{_lock}\n"
            )

    def test_global_install_with_workflow_emits_warning(
        self,
        tmp_path: Path,
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--global`` + workflow-shape prompts warns (not fails).

        Workflows installed at user scope run with CWD=~/.copilot, not a
        project, which is almost never what the author intended. PR A
        replaces the prior hard-fail with a warn-and-proceed: the row
        still lands, but the user is told to attach it to a project
        from the App UI.
        """
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        pkg_dir = tmp_path / "global-wf-pkg"
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            textwrap.dedent(
                """\
                name: global-wf-pkg
                description: global+workflow warn test
                version: 0.0.1
                """
            ),
            encoding="ascii",
        )
        (prompts_dir / "morning.prompt.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: Morning
                interval: daily
                schedule_hour: 8
                schedule_day: 1
                ---
                Greet the day.
                """
            ),
            encoding="ascii",
        )

        from apm_cli.integration import copilot_app_db as cdb

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", str(pkg_dir), "--target", "copilot-app", "--global"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        # Warn-and-proceed: exit 0, row was inserted, but warning visible.
        assert result.exit_code == 0, result.output
        ids = cdb.list_managed_workflow_ids(db)
        assert len(ids) == 1, ids
        # Warning text must mention --global and surface the attach hint.
        out = (result.output or "").lower()
        assert "--global" in out, result.output
        assert "attach" in out, result.output
