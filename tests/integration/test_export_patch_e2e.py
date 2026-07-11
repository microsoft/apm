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
        assert not (project[0] / "apm-patches").exists()

    def test_closed_loop_edit_export_apply_converges(self, project, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not available")
        proj, pkg_src = project

        # 1. A local edit to the deployed managed file.
        (proj / _DEPLOYED).write_bytes(_EDITED)

        # 2. Export it.
        result = _run(["export-patch", "-o", "patches"])
        assert result.exit_code == 0, result.output
        patch_file = proj / "patches" / "testorg-testpkg.patch"
        assert patch_file.exists()
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

        result = _run(["export-patch", "--dry-run", "-o", "patches"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert not (proj / "patches").exists()

    def test_missing_cache_fails_with_guidance(self, project) -> None:
        proj, pkg_src = project
        (proj / _DEPLOYED).write_bytes(_EDITED)
        shutil.rmtree(pkg_src)

        result = _run(["export-patch"])
        assert result.exit_code == 1
        assert "apm install" in result.output
