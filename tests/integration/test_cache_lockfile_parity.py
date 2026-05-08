"""Lockfile-determinism integration test under the persistent cache.

Regression-trap for the worst silent failure the cache layer could
introduce: lockfile drift between cached and non-cached runs. If
``apm install`` produces a different ``apm.lock.yaml`` (modulo the
``generated_at`` write-timestamp) when ``APM_NO_CACHE=1`` is set vs.
when the cache is hot, a CI run that ships with a stale cache would
commit a lockfile that disagrees with the reproducible-from-scratch
baseline -- and downstream installs would diverge.

The contract: ``apm install`` from the same ``apm.yml`` MUST produce
a content-identical lockfile (excluding ``generated_at``) regardless
of cache state. This test asserts it across three regimes:

  Run A: cold cache (cache empty)
  Run B: cache hot (warm reuse, no network for unchanged deps)
  Run C: APM_NO_CACHE=1 (cache layer disabled entirely)

A.lock == B.lock == C.lock is the parity invariant.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command() -> str:
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def project_with_apm(tmp_path: Path) -> Path:
    """Minimal APM project with one stable APM dep for parity checks."""
    project = tmp_path / "parity-test"
    project.mkdir()
    (project / ".github").mkdir()
    (project / "apm.yml").write_text(
        """\
name: parity-test
version: 0.1.0
target: copilot
dependencies:
  apm:
    - microsoft/apm-sample-package
""",
        encoding="utf-8",
    )
    return project


def _run_install(apm: str, project: Path, *, env_overrides: dict[str, str]) -> None:
    env = os.environ.copy()
    env.update(env_overrides)
    # Quiet output keeps the test fast and avoids parsing fragility.
    result = subprocess.run(
        [apm, "install"],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, (
        f"apm install failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def _lockfile_sha(project: Path) -> str:
    """Hash the lockfile excluding the `generated_at` line.

    `generated_at` is a wall-clock timestamp captured at write time, so it
    necessarily differs between independent runs. The parity invariant is
    about resolution outcome (resolved_commit, content_hash, deployed_files,
    package_type, ...), not the write timestamp.
    """
    lock = project / "apm.lock.yaml"
    assert lock.is_file(), "apm.lock.yaml not produced by install"
    canonical = "\n".join(
        line
        for line in lock.read_text(encoding="utf-8").splitlines()
        if not line.startswith("generated_at:")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clone_project(template: Path, dest: Path) -> Path:
    """Clone *template* into *dest* so each regime runs against pristine state.

    Reusing the same project dir across regimes would leave previously-deployed
    integration outputs (`.github/agents/`, `.agents/skills/`, ...) on disk.
    With the lockfile deleted between runs, those orphaned files look like
    user-authored collisions to the integrators (`check_collision()` returns
    True), so they are skipped and never recorded in the new lockfile's
    `deployed_files`. That is a fixture artifact, not a cache-layer drift, so
    we sidestep it by giving each regime its own copy of the project tree.
    """
    shutil.copytree(template, dest)
    return dest


def test_lockfile_byte_identical_across_cache_regimes(
    apm_command: str,
    project_with_apm: Path,
    tmp_path: Path,
) -> None:
    """A, B, C must produce content-identical apm.lock.yaml (modulo `generated_at`).

    A: cold cache (fresh APM_CACHE_DIR pointing at empty dir)
    B: warm cache (same dir, second run reuses entries)
    C: cache disabled (APM_NO_CACHE=1)
    """
    cache_dir = tmp_path / "isolated-cache"
    cache_dir.mkdir()

    # Run A: cold cache
    project_a = _clone_project(project_with_apm, tmp_path / "run-a")
    _run_install(
        apm_command,
        project_a,
        env_overrides={"APM_CACHE_DIR": str(cache_dir), "CI": "1"},
    )
    sha_a = _lockfile_sha(project_a)

    # Run B: warm cache (same APM_CACHE_DIR retained, fresh project tree)
    project_b = _clone_project(project_with_apm, tmp_path / "run-b")
    _run_install(
        apm_command,
        project_b,
        env_overrides={"APM_CACHE_DIR": str(cache_dir), "CI": "1"},
    )
    sha_b = _lockfile_sha(project_b)

    # Run C: cache disabled (fresh project tree)
    project_c = _clone_project(project_with_apm, tmp_path / "run-c")
    _run_install(
        apm_command,
        project_c,
        env_overrides={"APM_NO_CACHE": "1", "CI": "1"},
    )
    sha_c = _lockfile_sha(project_c)

    assert sha_a == sha_b, (
        "Lockfile drifted between cold-cache and warm-cache runs -- "
        "the cache layer is mutating resolution results."
    )
    assert sha_a == sha_c, (
        "Lockfile drifted between cached and APM_NO_CACHE=1 runs -- "
        "the cache layer is producing a different lockfile than the "
        "no-cache reference path."
    )
