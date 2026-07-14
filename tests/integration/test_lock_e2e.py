"""Integration tests for the ``apm lock`` command.

Issue: https://github.com/microsoft/apm/issues/975

Validates the end-to-end behaviour of ``apm lock``:

* A project with no dependencies produces a valid ``apm.lock.yaml`` and
  exits 0.
* ``apm lock`` with a local-path dependency writes the dependency entry
  to the lockfile without deploying any files.
* The lockfile is written to the project root and is idempotent
  (running again with unchanged deps overwrites with the same content).

These tests are hermetic: no network access is required.  Local-path
dependencies are used wherever a real dependency is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_apm_binary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def apm_command(apm_binary_path: Path) -> str:
    return str(apm_binary_path)


def _run_apm(
    apm_command: str,
    args: list[str],
    cwd: Path,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [apm_command, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_apm_yml(project_dir: Path, deps: list[str] | None = None) -> None:
    config: dict = {
        "name": "lock-test-project",
        "version": "1.0.0",
        "dependencies": {"apm": deps or [], "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _make_local_package(pkg_dir: Path, name: str) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "apm.yml").write_text(
        yaml.dump(
            {
                "name": name,
                "version": "1.0.0",
                "description": f"Local test package {name}",
            }
        ),
        encoding="utf-8",
    )
    instructions = pkg_dir / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / "test.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Test\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLockEmptyDeps:
    def test_no_deps_creates_lockfile_and_exits_zero(
        self, apm_command: str, tmp_path: Path
    ) -> None:
        """A project with no dependencies should produce apm.lock.yaml and
        exit 0 -- the core promise of the lockfile-only path."""
        project = tmp_path / "project"
        project.mkdir()
        _write_apm_yml(project)

        result = _run_apm(apm_command, ["lock"], project)

        assert result.returncode == 0, (
            f"apm lock exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        lockfile = project / "apm.lock.yaml"
        assert lockfile.exists(), "apm.lock.yaml must be created even with no dependencies"

    def test_no_deps_lockfile_is_valid_yaml(self, apm_command: str, tmp_path: Path) -> None:
        """The produced lockfile must be parseable YAML."""
        project = tmp_path / "project"
        project.mkdir()
        _write_apm_yml(project)

        _run_apm(apm_command, ["lock"], project)

        lockfile = project / "apm.lock.yaml"
        content = lockfile.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), "apm.lock.yaml must be a YAML mapping"


class TestLockLocalDep:
    def test_local_dep_writes_lockfile_without_deploying(
        self, apm_command: str, tmp_path: Path
    ) -> None:
        """apm lock with a local-path dep must write the lockfile but NOT
        copy any files to .github/, .agents/, or similar harness dirs."""
        project = tmp_path / "project"
        project.mkdir()

        pkg_dir = tmp_path / "my-skills"
        _make_local_package(pkg_dir, "my-skills")

        _write_apm_yml(project, deps=[str(pkg_dir)])

        result = _run_apm(apm_command, ["lock"], project)

        assert result.returncode == 0, (
            f"apm lock exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        lockfile = project / "apm.lock.yaml"
        assert lockfile.exists(), "apm.lock.yaml must be written for local deps"

        github_dir = project / ".github"
        assert not github_dir.exists(), (
            "apm lock must NOT deploy files to .github/ -- only the lockfile should be written"
        )
        agents_dir = project / ".agents"
        assert not agents_dir.exists(), (
            "apm lock must NOT deploy files to .agents/ -- only the lockfile should be written"
        )

    def test_local_dep_lockfile_records_dependency(self, apm_command: str, tmp_path: Path) -> None:
        """The lockfile written by apm lock must record the local dependency."""
        project = tmp_path / "project"
        project.mkdir()

        pkg_dir = tmp_path / "my-skills"
        _make_local_package(pkg_dir, "my-skills")

        _write_apm_yml(project, deps=[str(pkg_dir)])
        _run_apm(apm_command, ["lock"], project)

        lockfile = project / "apm.lock.yaml"
        content = lockfile.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict)
        deps = parsed.get("dependencies", {})
        assert len(deps) > 0, "lockfile must record at least one dependency entry for the local dep"

    def test_lock_is_idempotent(self, apm_command: str, tmp_path: Path) -> None:
        """Running apm lock twice with unchanged deps produces the same lockfile."""
        project = tmp_path / "project"
        project.mkdir()

        pkg_dir = tmp_path / "my-skills"
        _make_local_package(pkg_dir, "my-skills")
        _write_apm_yml(project, deps=[str(pkg_dir)])

        _run_apm(apm_command, ["lock"], project)
        first = (project / "apm.lock.yaml").read_text(encoding="utf-8")

        _run_apm(apm_command, ["lock"], project)
        second = (project / "apm.lock.yaml").read_text(encoding="utf-8")

        assert first == second, "apm lock must be idempotent -- same lockfile on second run"
