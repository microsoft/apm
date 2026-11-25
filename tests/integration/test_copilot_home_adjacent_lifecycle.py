"""Copilot-adjacent surfaces stay put when ``COPILOT_HOME`` moves (#3745, 0.36.1).

Approved decision D1 relocates only Copilot CLI user-scope files. The Copilot
desktop App (``~/.copilot/data.db`` workflows) is out of scope and must keep
its location, and ``compile --global`` (no Copilot user compile output) must
not start writing into either Copilot home. Regression locks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import HarnessScene, describe
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

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


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    try:
        yield result
    finally:
        result.close()


def _files(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_copilot_app_workflows_stay_in_the_default_copilot_home(scene: HarnessScene) -> None:
    copilot_home = scene.isolated.root / "profiles" / "copilot-home"
    env = {**scene.env, "COPILOT_HOME": str(copilot_home)}
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir()
    db = scene.home / ".copilot" / "data.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.executescript(_WORKFLOWS_SCHEMA)
    connection.execute("PRAGMA user_version = 13")
    connection.commit()
    connection.close()
    factory = LocalPackageFactory(scene.isolated.package_root)
    package = factory.create("app-kit")
    factory.add_prompt(
        package,
        "daily-digest",
        "---\nname: Daily Digest\ninterval: daily\nschedule_hour: 9\nschedule_day: 1\n"
        "mode: interactive\n---\nSummarise yesterday.\n",
    )
    run = scene.runner.run
    enabled = run(("experimental", "enable", "copilot-app"), cwd=cwd, env=env)
    assert enabled.returncode == 0, describe(enabled)

    result = run(
        ("install", "-g", str(package.root), "--no-policy", "--target", "copilot-app"),
        cwd=cwd,
        env=env,
    )

    assert result.returncode == 0, describe(result)
    connection = sqlite3.connect(db)
    rows = connection.execute("SELECT id FROM workflows").fetchall()
    connection.close()
    assert rows, describe(result)
    assert _files(copilot_home) == set(), _files(copilot_home)


def test_compile_global_writes_into_neither_copilot_home(scene: HarnessScene) -> None:
    copilot_home = scene.isolated.root / "profiles" / "copilot-home"
    env = {**scene.env, "COPILOT_HOME": str(copilot_home)}
    root = scene.isolated.config_root
    (root / "apm.yml").write_text(
        "name: user-profile\nversion: 1.0.0\ntargets:\n  - copilot\n  - claude\n"
        "dependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    (root / ".apm" / "instructions").mkdir(parents=True)
    (root / ".apm" / "instructions" / "house.instructions.md").write_text(
        "---\napplyTo: '**'\ndescription: house rules\n---\nBe concise.\n", encoding="utf-8"
    )
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir()
    installed = scene.runner.run(("install", "-g", "--no-policy"), cwd=cwd, env=env)
    assert installed.returncode == 0, describe(installed)
    before = {name: (copilot_home / name).read_bytes() for name in _files(copilot_home)}

    result = scene.runner.run(("compile", "--global"), cwd=cwd, env=env)

    assert result.returncode == 0, describe(result)
    assert {name: (copilot_home / name).read_bytes() for name in _files(copilot_home)} == before
    assert _files(scene.home / ".copilot") == set()
