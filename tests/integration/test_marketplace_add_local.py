"""End-to-end integration: ``apm marketplace add`` against a real local repo.

Exercises the full add -> persist -> read-back -> fetch flow against:
- a working-directory checkout (direct file read)
- a bare repo (``git show`` path)

Drives the CLI through ``click.testing.CliRunner`` against an isolated
``$HOME`` so no real user config is touched. Requires the ``git``
executable in PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace as marketplace_group
from apm_cli.marketplace import registry
from apm_cli.marketplace.client import fetch_or_cache

GIT_AVAILABLE = shutil.which("git") is not None

pytestmark = pytest.mark.skipif(not GIT_AVAILABLE, reason="git executable not available")

MANIFEST = {
    "name": "test-mkt",
    "owner": "test",
    "plugins": [
        {"name": "skill-a", "source": "./skills/skill-a", "version": "0.1.0"},
    ],
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = str(tmp_path / ".apm")
    Path(config_dir).mkdir()
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr(registry, "_registry_cache", None)


def _seed_working_dir_repo(repo: Path) -> None:
    """Create a working-dir git repo with marketplace.json committed to main."""
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    (repo / "marketplace.json").write_text(json.dumps(MANIFEST))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env=env,
    )


def _make_bare_clone(working: Path, bare: Path) -> None:
    subprocess.run(
        ["git", "clone", "--bare", str(working), str(bare)],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("topology", ["working", "bare"])
def test_marketplace_add_local_path(topology: str, tmp_path: Path) -> None:
    working = tmp_path / "mkt-src"
    _seed_working_dir_repo(working)
    if topology == "bare":
        bare = tmp_path / "mkt.git"
        _make_bare_clone(working, bare)
        path = bare
    else:
        path = working

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", str(path), "--name", "local-mkt"])
    assert result.exit_code == 0, result.output

    sources = registry.get_registered_marketplaces()
    assert len(sources) == 1
    src = sources[0]
    assert src.name == "local-mkt"
    assert src.kind == "local"
    assert src.url.startswith("file://")

    manifest = fetch_or_cache(src)
    assert manifest.name == "test-mkt"
    assert len(manifest.plugins) == 1
    assert manifest.plugins[0].name == "skill-a"


def test_marketplace_add_file_uri(tmp_path: Path) -> None:
    working = tmp_path / "mkt-src"
    _seed_working_dir_repo(working)
    file_uri = f"file://{working}"

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", file_uri, "--name", "uri-mkt"])
    assert result.exit_code == 0, result.output

    sources = registry.get_registered_marketplaces()
    assert sources[0].kind == "local"


def test_marketplace_add_local_list_shows_path(tmp_path: Path) -> None:
    """``list`` after registering a local marketplace renders ``display_source``, not blank owner/repo."""
    working = tmp_path / "mkt-src"
    _seed_working_dir_repo(working)
    runner = CliRunner()
    runner.invoke(marketplace_group, ["add", str(working), "--name", "local-mkt"])
    result = runner.invoke(marketplace_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "local-mkt" in result.output
    # The path may be truncated by the Rich table; check for either a path prefix
    # or that "Source" column is rendered with a slash-like content (not empty owner/repo).
    assert "/" in result.output  # local path renders
    assert "Ref" in result.output  # renamed column from "Branch"
