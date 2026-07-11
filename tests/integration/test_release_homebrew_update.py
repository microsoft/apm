"""Regression coverage for the Homebrew release ownership boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_homebrew_update_commits_without_cross_repo_auth(tmp_path: Path) -> None:
    """Run a local tap commit after guarding the release workflow auth boundary."""
    repo_root = Path(__file__).parents[2]
    tap_repo = tmp_path / "homebrew-apm"
    formula_dir = tap_repo / "Formula"
    formula_dir.mkdir(parents=True)
    formula = formula_dir / "apm.rb"
    formula.write_text(
        "class Apm < Formula\n"
        '  version "0.0.1"\n'
        '  url "https://example.invalid/apm-v0.0.1.tar.gz"\n'
        f'  sha256 "{"0" * 64}"\n'
        "end\n",
        encoding="utf-8",
    )
    _git(tap_repo, "init", "-b", "main")
    _git(tap_repo, "config", "user.name", "Homebrew update harness")
    _git(tap_repo, "config", "user.email", "harness@example.invalid")
    _git(tap_repo, "add", "Formula/apm.rb")
    _git(tap_repo, "commit", "-m", "Initial formula")

    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "tag_name": "v9.8.7",
                "asset_url": "https://github.com/microsoft/apm/releases/download/v9.8.7/apm.tar.gz",
                "sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh_marker = tmp_path / "gh-was-called"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        f"#!/bin/sh\nprintf called > {gh_marker}\nexit 97\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env.pop("GH_PKG_PAT", None)
    env.pop("GH_TOKEN", None)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("release_homebrew_update_harness.py")),
            "--workflow",
            str(repo_root / ".github" / "workflows" / "build-release.yml"),
            "--release",
            str(release),
            "--tap-repo",
            str(tap_repo),
        ],
        cwd=repo_root,
        env=env,
        check=True,
    )

    updated = formula.read_text(encoding="utf-8")
    assignments = {
        line.strip().split(" ", 1)[0]: line.strip().split('"')[1]
        for line in updated.splitlines()
        if line.strip().startswith(("version ", "url ", "sha256 "))
    }
    assert assignments["version"] == "9.8.7"
    parsed = urlparse(assignments["url"])
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        "github.com",
        "/microsoft/apm/releases/download/v9.8.7/apm.tar.gz",
    )
    assert assignments["sha256"] == "a" * 64
    assert _git(tap_repo, "rev-list", "--count", "HEAD") == "2"
    assert _git(tap_repo, "log", "-1", "--pretty=%s") == "Update APM to v9.8.7"
    assert not gh_marker.exists()
