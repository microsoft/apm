"""Integration test for ``apm deps why`` against a real on-disk lockfile.

Builds a multi-level dependency graph in a temp directory, writes it as an
``apm.lock.yaml`` artifact, and invokes the actual CLI -- no network, no
git, just the real file-load + walker + renderer pipeline.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LOCKFILE_NAME, LockedDependency, LockFile


@contextlib.contextmanager
def _cwd(path: Path):
    original = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original)


@pytest.fixture
def multi_level_project(tmp_path: Path) -> Path:
    """Project with a 3-level dep chain plus a sibling chain.

    Forward graph:
        acme/big-skills (direct)
          -> acme/shared-utils (transitive depth 2)
              -> acme/deep-core (transitive depth 3)
        acme/other-skills (direct)
          -> acme/shared-utils  (recorded once -- the lockfile has a
             single shared-utils entry with one resolved_by)
    """
    lf = LockFile()
    lf.add_dependency(
        LockedDependency(
            repo_url="acme/big-skills",
            version="1.2.4",
            depth=1,
            resolved_by=None,
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="acme/other-skills",
            version="0.9.1",
            depth=1,
            resolved_by=None,
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="acme/shared-utils",
            version="1.4.2",
            depth=2,
            resolved_by="acme/big-skills",
        )
    )
    lf.add_dependency(
        LockedDependency(
            repo_url="acme/deep-core",
            version="3.1.0",
            depth=3,
            resolved_by="acme/shared-utils",
        )
    )
    (tmp_path / LOCKFILE_NAME).write_text(lf.to_yaml(), encoding="utf-8")
    return tmp_path


def test_why_transitive_chain_renders_full_path(multi_level_project: Path):
    """End-to-end: deep transitive resolves to a 3-link chain."""
    runner = CliRunner()
    with _cwd(multi_level_project):
        result = runner.invoke(cli, ["deps", "why", "deep-core"])
    assert result.exit_code == 0, result.output
    # Header line names the package and identifies as transitive.
    assert "acme/deep-core@3.1.0" in result.output
    assert "(transitive)" in result.output
    # Each ancestor up the chain appears in the rendered output.
    assert "acme/big-skills" in result.output
    assert "acme/shared-utils" in result.output
    # ASCII tree connector (no unicode box-drawing chars)
    assert "+--" in result.output
    assert "\u2500" not in result.output  # no rich box-drawing


def test_why_json_snapshot_stable(multi_level_project: Path):
    """JSON schema is stable and well-formed for downstream tooling."""
    runner = CliRunner()
    with _cwd(multi_level_project):
        result = runner.invoke(cli, ["deps", "why", "deep-core", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["package"]["repo_url"] == "acme/deep-core"
    assert payload["package"]["is_direct"] is False
    assert len(payload["paths"]) == 1
    chain = payload["paths"][0]["chain"]
    assert [link["repo_url"] for link in chain] == [
        "acme/big-skills",
        "acme/shared-utils",
        "acme/deep-core",
    ]
    assert chain[0]["is_direct"] is True
    assert chain[-1]["is_direct"] is False


def test_why_direct_dep_end_to_end(multi_level_project: Path):
    runner = CliRunner()
    with _cwd(multi_level_project):
        result = runner.invoke(cli, ["deps", "why", "big-skills"])
    assert result.exit_code == 0, result.output
    assert "(direct dependency)" in result.output


def test_why_missing_lockfile_exits_2(tmp_path: Path):
    runner = CliRunner()
    empty_project = tmp_path / "empty"
    empty_project.mkdir()
    with _cwd(empty_project):
        result = runner.invoke(cli, ["deps", "why", "anything"])
    assert result.exit_code == 2
