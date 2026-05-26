"""End-to-end: ``apm marketplace add`` against a ``file://`` bare repo URL.

``file://`` URIs to bare repos classify as ``kind="local"`` (see
``_looks_like_local_marketplace_source``) and route through the local fetcher
(``_fetch_local`` -> ``git show``). This test exercises that path end-to-end
through ``apm marketplace add`` + ``fetch_or_cache``.

For coverage of the generic-git path (``kind="git"`` -> ``_fetch_git`` ->
``GitCache``), see the unit tests in ``tests/unit/marketplace/test_client_git.py``
and ``test_resolver_local_git.py``; a hermetic integration test would need a
running git daemon and is deliberately out of scope here.
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
    "name": "gen-mkt",
    "owner": "test",
    "plugins": [
        {"name": "tool-x", "source": "./tools/x", "version": "1.0.0"},
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
    # Redirect APM cache root so GitCache doesn't write to the real user cache.
    monkeypatch.setenv("APM_HOME", str(tmp_path / ".apm"))


def _seed_bare_repo(working: Path, bare: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    working.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(working)], check=True, capture_output=True, env=env
    )
    (working / "marketplace.json").write_text(json.dumps(MANIFEST))
    subprocess.run(["git", "-C", str(working), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(working), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "clone", "--bare", str(working), str(bare)],
        check=True,
        capture_output=True,
        env=env,
    )


def test_marketplace_add_file_uri_to_bare_repo(tmp_path: Path) -> None:
    """Register a marketplace via a ``file://`` URL pointing at a bare repo.

    ``file://`` URIs classify as ``kind="local"`` and route through
    ``_fetch_local`` (which uses ``git show`` against the bare repo). This
    is the documented behaviour: any ``file://`` URI -- whether pointing at
    a bare or working repo -- always routes through the local fetcher.
    """
    working = tmp_path / "src"
    bare = tmp_path / "mkt.git"
    _seed_bare_repo(working, bare)

    # file:// URI to the bare repo
    file_uri = f"file://{bare}"

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", file_uri, "--name", "gen-mkt"])
    assert result.exit_code == 0, result.output

    sources = registry.get_registered_marketplaces()
    assert len(sources) == 1
    src = sources[0]
    assert src.name == "gen-mkt"
    # file:// URIs to bare repos are classified as "local" by the parser; the
    # _fetch_local fetcher handles them via "git show". This is the documented
    # behaviour -- file:// always routes through the local fetcher.
    assert src.kind == "local"

    manifest = fetch_or_cache(src)
    assert manifest.name == "gen-mkt"
    assert manifest.plugins[0].name == "tool-x"
