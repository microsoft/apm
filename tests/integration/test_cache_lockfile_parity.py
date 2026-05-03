"""Lockfile-determinism integration test under the persistent cache.

Regression-trap for the worst silent failure the cache layer could
introduce: byte-level lockfile drift between cached and non-cached
runs. If ``apm install`` produces a different ``apm.lock.yaml`` when
``APM_NO_CACHE=1`` is set vs. when the cache is hot, a CI run that
ships with a stale cache would commit a lockfile that disagrees with
the reproducible-from-scratch baseline -- and downstream installs
would diverge.

The contract: ``apm install`` from the same ``apm.yml`` MUST produce
a byte-identical lockfile regardless of cache state. This test
asserts it across three regimes:

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
    lock = project / "apm.lock.yaml"
    assert lock.is_file(), "apm.lock.yaml not produced by install"
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def _reset_install_state(project: Path) -> None:
    """Remove install artifacts but keep apm.yml so the next run is identical input."""
    for child in (project / "apm_modules", project / "apm.lock.yaml"):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_file():
            child.unlink()


def test_lockfile_byte_identical_across_cache_regimes(
    apm_command: str,
    project_with_apm: Path,
    tmp_path: Path,
) -> None:
    """A, B, C must produce byte-identical apm.lock.yaml.

    A: cold cache (fresh APM_CACHE_DIR pointing at empty dir)
    B: warm cache (same dir, second run reuses entries)
    C: cache disabled (APM_NO_CACHE=1)
    """
    cache_dir = tmp_path / "isolated-cache"
    cache_dir.mkdir()

    # Run A: cold cache
    _run_install(
        apm_command,
        project_with_apm,
        env_overrides={"APM_CACHE_DIR": str(cache_dir), "CI": "1"},
    )
    sha_a = _lockfile_sha(project_with_apm)

    # Run B: warm cache (same APM_CACHE_DIR retained)
    _reset_install_state(project_with_apm)
    _run_install(
        apm_command,
        project_with_apm,
        env_overrides={"APM_CACHE_DIR": str(cache_dir), "CI": "1"},
    )
    sha_b = _lockfile_sha(project_with_apm)

    # Run C: cache disabled
    _reset_install_state(project_with_apm)
    _run_install(
        apm_command,
        project_with_apm,
        env_overrides={"APM_NO_CACHE": "1", "CI": "1"},
    )
    sha_c = _lockfile_sha(project_with_apm)

    assert sha_a == sha_b, (
        "Lockfile drifted between cold-cache and warm-cache runs -- "
        "the cache layer is mutating resolution results."
    )
    assert sha_a == sha_c, (
        "Lockfile drifted between cached and APM_NO_CACHE=1 runs -- "
        "the cache layer is producing a different lockfile than the "
        "no-cache reference path."
    )
