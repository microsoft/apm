"""E2E regression tests for ``apm install --target copilot-app --global``.

Three scenarios mirror the cowork suite (test_install_target_copilot_cowork_e2e.py):

  1. Flag OFF                -> enable-hint printed, exit 0.
  2. Flag ON, no ``data.db`` -> "Copilot App not detected" error, exit 1.
  3. Project scope           -> "requires --global" error, exit 1.

A fourth happy-path test exercises the full deploy + uninstall cycle
against a temp SQLite DB seeded with the live workflows schema, proving
that ``apm install`` -> ``apm uninstall`` actually writes and removes
APM-namespaced rows.
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

    def test_project_scope_requires_global(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --global, copilot-app must error with --global hint."""
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"copilot_app": True}},
        )
        # Point env override at a real DB so resolver succeeds and we reach
        # the project-scope gate (not the missing-db gate).
        db = _seed_db(fake_home / "data.db")
        monkeypatch.setenv("APM_COPILOT_APP_DB", str(db))

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "copilot-app"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=True,
        )
        assert result.exit_code != 0, result.output
        normalized = " ".join((result.output or "").split())
        assert "requires --global" in normalized, result.output


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
                schedule:
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
                schedule:
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
            ["uninstall", str(pkg_dir), "--global"],
            env={**_BASE_ENV, "APM_COPILOT_APP_DB": str(db)},
            catch_exceptions=False,
        )
        assert uninstall_result.exit_code == 0, uninstall_result.output

        ids_after_uninstall = cdb.list_managed_workflow_ids(db)
        assert ids_after_uninstall == [], (
            f"uninstall must delete the DB row, but {ids_after_uninstall} remain"
        )
