"""Tests for ``apm deps why`` -- CLI surface and exit codes."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LOCKFILE_NAME, LockedDependency, LockFile


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@contextlib.contextmanager
def _cwd(path: Path):
    original = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original)


def _make_lockfile(deps: list[LockedDependency]) -> LockFile:
    lf = LockFile()
    for d in deps:
        lf.add_dependency(d)
    return lf


def _write_lockfile(tmp: Path, lf: LockFile) -> Path:
    p = tmp / LOCKFILE_NAME
    p.write_text(lf.to_yaml(), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------


class TestWhyHumanOutput:
    def test_why_direct_dep_human_output(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/big",
                        version="1.2.4",
                        depth=1,
                        resolved_by=None,
                    )
                ]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "acme/big"])
            assert result.exit_code == 0, result.output
            assert "acme/big@1.2.4" in result.output
            assert "(direct dependency)" in result.output
            assert "declared in apm.yml" in result.output

    def test_why_transitive_dep_human_output(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/big", version="1.2.4", depth=1, resolved_by=None
                    ),
                    LockedDependency(
                        repo_url="acme/util",
                        version="1.4.2",
                        depth=2,
                        resolved_by="acme/big",
                    ),
                ]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "acme/util"])
            assert result.exit_code == 0, result.output
            assert "acme/util@1.4.2" in result.output
            assert "(transitive)" in result.output
            assert "acme/big" in result.output
            # ASCII tree marker (no Unicode box-drawing)
            assert "+--" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestWhyJsonOutput:
    def test_why_json_output_matches_schema(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/big", version="1.2.4", depth=1, resolved_by=None
                    ),
                    LockedDependency(
                        repo_url="acme/util",
                        version="1.4.2",
                        depth=2,
                        resolved_by="acme/big",
                    ),
                ]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "acme/util", "--json"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            assert payload["package"]["repo_url"] == "acme/util"
            assert payload["package"]["is_direct"] is False
            assert len(payload["paths"]) == 1
            chain = payload["paths"][0]["chain"]
            assert chain[0]["repo_url"] == "acme/big"
            assert chain[0]["is_direct"] is True
            assert chain[-1]["repo_url"] == "acme/util"

    def test_why_json_not_installed_emits_error_payload(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [LockedDependency(repo_url="acme/big", version="1.2.4", depth=1, resolved_by=None)]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "nope", "--json"])
            assert result.exit_code == 1
            payload = json.loads(result.output)
            assert payload == {"error": "not_installed", "query": "nope"}


# ---------------------------------------------------------------------------
# Error paths and exit codes
# ---------------------------------------------------------------------------


class TestWhyErrorPaths:
    def test_why_ambiguous_query_exits_1(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/shared-utils",
                        version="1.0.0",
                        depth=1,
                        resolved_by=None,
                    ),
                    LockedDependency(
                        repo_url="other/shared-utils",
                        version="2.0.0",
                        depth=1,
                        resolved_by=None,
                    ),
                ]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "shared-utils"])
            assert result.exit_code == 1
            assert "multiple packages" in result.output

    def test_why_not_installed_exits_1(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/big",
                        version="1.0",
                        depth=1,
                        resolved_by=None,
                    )
                ]
            )
            _write_lockfile(tmp_path, lf)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "missing-pkg"])
            assert result.exit_code == 1
            assert "not installed" in result.output

    def test_why_no_lockfile_exits_2(self, runner):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with _cwd(tmp_path):
                result = runner.invoke(cli, ["deps", "why", "anything"])
            assert result.exit_code == 2
            assert "no apm.lock.yaml" in result.output


# ---------------------------------------------------------------------------
# --global flag
# ---------------------------------------------------------------------------


class TestWhyGlobalFlag:
    def test_why_global_flag_uses_user_scope_lockfile(self, runner):
        with tempfile.TemporaryDirectory() as user_home:
            user_path = Path(user_home)
            user_apm = user_path / ".apm"
            user_apm.mkdir()
            lf = _make_lockfile(
                [
                    LockedDependency(
                        repo_url="acme/global-pkg",
                        version="0.1.0",
                        depth=1,
                        resolved_by=None,
                    )
                ]
            )
            _write_lockfile(user_apm, lf)

            with patch("pathlib.Path.home", return_value=user_path):
                result = runner.invoke(cli, ["deps", "why", "global-pkg", "--global"])
            assert result.exit_code == 0, result.output
            assert "acme/global-pkg" in result.output


# ---------------------------------------------------------------------------
# No-network regression trap
# ---------------------------------------------------------------------------


class TestWhyNoNetwork:
    def test_why_command_does_not_hit_network(self, runner):
        """The command must be offline; assert subprocess.run is never called."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lf = _make_lockfile(
                [
                    LockedDependency(repo_url="acme/big", version="1.0", depth=1, resolved_by=None),
                    LockedDependency(
                        repo_url="acme/util",
                        version="1.4",
                        depth=2,
                        resolved_by="acme/big",
                    ),
                ]
            )
            _write_lockfile(tmp_path, lf)
            with (
                _cwd(tmp_path),
                patch("subprocess.run") as mock_run,
                patch("urllib.request.urlopen") as mock_urlopen,
            ):
                result_human = runner.invoke(cli, ["deps", "why", "acme/util"])
                result_json = runner.invoke(cli, ["deps", "why", "acme/util", "--json"])
            assert result_human.exit_code == 0, result_human.output
            assert result_json.exit_code == 0, result_json.output
            mock_run.assert_not_called()
            mock_urlopen.assert_not_called()
