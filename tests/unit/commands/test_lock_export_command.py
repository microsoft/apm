"""Unit tests for the ``apm lock export`` subcommand (issue #1777, U5).

Covers: format selection, stdout vs --output, reads the existing lockfile only
(no resolve), reproducible timestamp pinning, and a clear error when no
lockfile exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

_LOCKFILE = """\
lockfile_version: "2"
generated_at: "2024-01-01T00:00:00+00:00"
dependencies:
  - repo_url: github.com/acme/git-utils
    resolved_commit: def789ghi012
    declared_license: MIT
  - repo_url: github.com/acme/undeclared
    resolved_commit: abc123
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed(project_dir: Path) -> None:
    (project_dir / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
    (project_dir / "apm.lock.yaml").write_text(_LOCKFILE)


def test_export_does_not_warn_about_undeclared_dep_licenses(runner, tmp_path):
    # Consuming-path silence (#1777 asymmetry): export must NOT nag about
    # transitive deps that lack a declared license -- it records NOASSERTION in
    # the SBOM silently. The authoring nudge fires only on apm pack/publish.
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())  # _LOCKFILE includes acme/undeclared (no license)
        result = runner.invoke(cli, ["lock", "export", "--format", "spdx"])
        assert result.exit_code == 0, result.stderr
        combined = result.stdout + (result.stderr or "")
        assert "Add a 'license:' field" not in combined
        assert "the SBOM will record NOASSERTION for this package" not in combined
    # `apm lock export | jq` must not be corrupted by a diagnostic on stdout:
    # when no lockfile exists the error must land on stderr, leaving stdout clean.
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["lock", "export"])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "No lockfile found" in result.stderr


def test_export_success_message_goes_to_stderr(runner, tmp_path):
    # With --output the SBOM goes to a file; the success diagnostic must route to
    # stderr so a piped stdout stays empty/clean.
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export", "-o", "sbom.json"])
        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""
        assert "SBOM written to" in result.stderr


def test_export_cyclonedx_to_stdout(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export", "--format", "cyclonedx"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["bomFormat"] == "CycloneDX"
        purls = {c["purl"] for c in doc["components"]}
        assert "pkg:github/acme/git-utils@def789ghi012" in purls


def test_export_spdx_to_stdout(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export", "--format", "spdx"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["spdxVersion"].startswith("SPDX-")


def test_export_defaults_to_cyclonedx(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["bomFormat"] == "CycloneDX"


def test_export_to_output_file(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export", "-o", "sbom.json"])
        assert result.exit_code == 0, result.output
        written = Path("sbom.json").read_text(encoding="utf-8")
        assert json.loads(written)["bomFormat"] == "CycloneDX"
        assert "SBOM written to sbom.json" in result.stderr


def test_export_missing_lockfile_errors(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        (Path.cwd() / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
        result = runner.invoke(cli, ["lock", "export"])
        assert result.exit_code == 1
        assert "No lockfile" in result.stderr


def test_export_does_not_resolve(runner, tmp_path, monkeypatch):
    # export must read the lockfile only -- never invoke the resolve pipeline.
    import apm_cli.commands.install as install_mod

    def _boom(*_a, **_k):
        raise AssertionError("export must not resolve")

    monkeypatch.setattr(install_mod, "_install_apm_dependencies", _boom, raising=False)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export"])
        assert result.exit_code == 0, result.output


def test_export_timestamp_is_reproducible(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        first = runner.invoke(cli, ["lock", "export", "--timestamp", "2030-01-01T00:00:00+00:00"])
        second = runner.invoke(cli, ["lock", "export", "--timestamp", "2030-01-01T00:00:00+00:00"])
        assert first.output == second.output
        assert "2030-01-01T00:00:00+00:00" in first.output


def test_export_undeclared_omits_licenses(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        _seed(Path.cwd())
        result = runner.invoke(cli, ["lock", "export"])
        doc = json.loads(result.output)
        undeclared = next(
            c for c in doc["components"] if c["purl"] == "pkg:github/acme/undeclared@abc123"
        )
        assert "licenses" not in undeclared
