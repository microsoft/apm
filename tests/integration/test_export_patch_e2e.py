"""End-to-end tests for ``apm export-patch``.

The core contract is the closed loop:

    edit a deployed managed file
    -> ``apm export-patch``
    -> ``git apply`` the patch in the package source
    -> the package source now carries the edit
    -> a replay against the updated source reports nothing to export

If any link in that chain breaks (reverse mapping, diff shape,
``git apply`` compatibility, header base), the loop does not converge
and the test fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.install.cache_pin import write_marker

_COMMIT = "b" * 40
_SOURCE_REL = ".apm/instructions/std.instructions.md"
_DEPLOYED = ".github/instructions/std.instructions.md"
_ORIGINAL = b'---\napplyTo: "**"\n---\n# Standard\n\nrule one\n'
_EDITED = b'---\napplyTo: "**"\n---\n# Standard\n\nrule one\nrule two\n'


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _make_project_with_remote_dep(tmp_path: Path) -> tuple[Path, Path]:
    """Fabricate a project whose lockfile pins one cached remote package.

    Mirrors the state ``apm install`` leaves behind for a GitHub dep:
    package snapshot under ``apm_modules/<owner>/<repo>`` with a cache-pin
    marker, the deployed file in the project tree, and a lockfile entry
    tracking it.
    """
    project = tmp_path / "proj"
    (project / ".github").mkdir(parents=True)
    (project / "apm.yml").write_bytes(
        yaml.safe_dump({"name": "proj", "version": "1.0.0", "target": "copilot"}).encode()
    )

    pkg_src = project / "apm_modules" / "testorg" / "testpkg"
    _write(pkg_src / "apm.yml", yaml.safe_dump({"name": "testpkg", "version": "1.0.0"}).encode())
    _write(pkg_src / _SOURCE_REL, _ORIGINAL)
    write_marker(pkg_src, _COMMIT)

    _write(project / _DEPLOYED, _ORIGINAL)

    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="testorg/testpkg",
            resolved_commit=_COMMIT,
            resolved_ref="main",
            version="1.0.0",
            deployed_files=[_DEPLOYED],
        )
    )
    lock.write(get_lockfile_path(project))
    return project, pkg_src


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    proj, pkg_src = _make_project_with_remote_dep(tmp_path)
    monkeypatch.chdir(proj)
    return proj, pkg_src


class TestExportPatchE2E:
    def test_clean_project_exports_nothing(self, project) -> None:
        result = _run(["export-patch"])
        assert result.exit_code == 0
        assert "nothing to export" in result.output.lower()
        assert not (project[0] / "patches").exists()

    def test_closed_loop_edit_export_apply_converges(self, project, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        proj, pkg_src = project

        # 1. A local edit to the deployed managed file.
        (proj / _DEPLOYED).write_bytes(_EDITED)

        # 2. Export it to the default patches/<pkg>@<ref>.patch location.
        lock_path = get_lockfile_path(proj)
        lock_before = lock_path.read_bytes()
        manifest_before = (proj / "apm.yml").read_bytes()
        result = _run(["export-patch"])
        assert result.exit_code == 0, result.output
        patch_file = proj / "patches" / "testorg__testpkg@main.patch"
        assert patch_file.exists()
        # Read-only: the edit, the lockfile and apm.yml are left untouched.
        assert (proj / _DEPLOYED).read_bytes() == _EDITED
        assert lock_path.read_bytes() == lock_before
        assert (proj / "apm.yml").read_bytes() == manifest_before
        patch_text = patch_file.read_text(encoding="utf-8")
        assert "# package: testorg/testpkg" in patch_text
        assert f"# base: commit {_COMMIT} (main)" in patch_text
        assert f"--- a/{_SOURCE_REL}\n" in patch_text

        # 3. Apply the patch in a clone of the package repository.
        upstream = tmp_path / "upstream"
        shutil.copytree(pkg_src, upstream, ignore=shutil.ignore_patterns(".apm-pin"))
        assert _git(upstream, "init", "-q").returncode == 0
        applied = _git(upstream, "apply", str(patch_file))
        assert applied.returncode == 0, applied.stderr

        # 4. The package source now carries the local edit. Compare after
        #    CRLF normalization: git on Windows may rewrite line endings
        #    on apply (core.autocrlf), which drift tolerates by design.
        patched = (upstream / _SOURCE_REL).read_bytes().replace(b"\r\n", b"\n")
        assert patched == _EDITED

        # 5. Convergence: ship the patched source (simulated by updating
        #    the cached snapshot) and the same command finds nothing left
        #    to export.
        (pkg_src / _SOURCE_REL).write_bytes(_EDITED)
        result = _run(["export-patch", "-o", "patches2"])
        assert result.exit_code == 0
        assert "nothing to export" in result.output.lower()
        assert not (proj / "patches2").exists()

    def test_dry_run_writes_nothing(self, project) -> None:
        proj, _pkg_src = project
        (proj / _DEPLOYED).write_bytes(_EDITED)

        result = _run(["export-patch", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert not (proj / "patches").exists()

    def test_out_option_overrides_directory(self, project) -> None:
        proj, _pkg_src = project
        (proj / _DEPLOYED).write_bytes(_EDITED)

        result = _run(["export-patch", "-o", "upstream-patches"])
        assert result.exit_code == 0, result.output
        assert (proj / "upstream-patches" / "testorg__testpkg@main.patch").exists()
        assert not (proj / "patches").exists()

    def test_missing_cache_fails_with_guidance(self, project) -> None:
        proj, pkg_src = project
        (proj / _DEPLOYED).write_bytes(_EDITED)
        shutil.rmtree(pkg_src)

        result = _run(["export-patch"])
        assert result.exit_code == 1
        assert "apm install" in result.output

    def test_audit_points_local_edits_at_export_patch(self, project) -> None:
        proj, _pkg_src = project
        (proj / _DEPLOYED).write_bytes(_EDITED)

        result = CliRunner().invoke(cli, ["audit", "--ci", "--no-policy"])
        output = " ".join(result.output.split())
        assert "1 local edit(s) to managed files" in output, result.output
        assert "run 'apm export-patch'" in output


class _StopBeforeDownload(Exception):
    pass


def test_install_points_local_edits_at_export_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real 'apm install' shows the hint once before it re-syncs files.

    The download phase is stubbed to stop the run right after the hint so
    the test stays hermetic; the edit must survive the aborted install.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    proj = tmp_path / "proj"
    (proj / ".github").mkdir(parents=True)
    (proj / "apm.yml").write_bytes(
        yaml.safe_dump(
            {
                "name": "proj",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": ["testorg/testpkg"]},
            }
        ).encode()
    )
    pkg_src = proj / "apm_modules" / "testorg" / "testpkg"
    _write(pkg_src / "apm.yml", yaml.safe_dump({"name": "testpkg", "version": "1.0.0"}).encode())
    _write(pkg_src / _SOURCE_REL, _ORIGINAL)
    write_marker(pkg_src, _COMMIT)
    _write(proj / _DEPLOYED, _ORIGINAL)
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="testorg/testpkg",
            host="github.com",
            resolved_commit=_COMMIT,
            version="1.0.0",
            deployed_files=[_DEPLOYED],
            deployed_file_hashes={_DEPLOYED: compute_file_hash(proj / _DEPLOYED)},
        )
    )
    lock.write(get_lockfile_path(proj))
    (proj / _DEPLOYED).write_bytes(_EDITED)
    monkeypatch.chdir(proj)

    def _stop(ctx) -> None:
        raise _StopBeforeDownload("stop before download")

    monkeypatch.setattr("apm_cli.install.phases.download.run", _stop)
    result = CliRunner().invoke(cli, ["install", "--frozen"])

    output = " ".join(result.output.split())
    assert output.count("apm export-patch") == 1, result.output
    assert "1 local edit(s) to managed files may be overwritten by this install" in output
    assert (proj / _DEPLOYED).read_bytes() == _EDITED
