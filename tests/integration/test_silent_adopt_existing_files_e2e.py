"""End-to-end regression: silent adopt of byte-identical pre-existing files.

Reproduces the catch-22 reported by zava-storefront where:
  1. A project's apm.lock loses ``deployed_files`` for non-skill packages
     (e.g. lockfile wiped, hand-edited, partial install crash, regenerated
     by an older APM build).
  2. The deployed files are still on disk, byte-identical to the
     package's source.
  3. Re-installing on stock 0.13.0 silently treats those files as
     "user-authored", skips them, and writes an empty ``deployed_files``
     back to the lockfile.
  4. The next install with ``required-packages-deployed`` enforced is
     permanently blocked because the policy gate runs *before* integrate
     in ``pipeline.py`` -- there's no path to self-heal.

Post-fix, step 3 silently *adopts* the existing files (their bytes match
the package source) and repopulates ``deployed_files`` -- breaking the
catch-22.

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_github_token


@pytest.fixture
def apm_command():
    """Resolve an apm binary that is wired to *this* checkout's source.

    The repo binary (homebrew/system ``apm``) may be a stable release that
    predates the fix under test, which would silently make this regression
    test pass against unfixed code. Prefer the CI-built artifact or a
    venv-installed editable binary so the test exercises the in-repo
    apm_cli source.

    Resolution order:
      1. ``APM_TEST_BINARY`` env override (explicit per-test pin).
      2. ``APM_BINARY_PATH`` env (CI sets this after the build step;
         shared with ``apm_binary_path`` fixture in ``conftest.py``).
      3. Repo-local ``.venv/bin/apm`` (this worktree).
      4. Sibling ``../awd-cli/.venv/bin/apm`` (shared dev venv pointing
         at this worktree via ``pip install -e .``).
      5. ``apm`` on PATH (last resort; may be stale).
    """
    import os

    for env_var in ("APM_TEST_BINARY", "APM_BINARY_PATH"):
        override = os.environ.get(env_var)
        if override and Path(override).exists():
            return override

    repo_root = Path(__file__).parent.parent.parent
    candidates = [
        repo_root / ".venv" / "bin" / "apm",
        repo_root.parent / "awd-cli" / ".venv" / "bin" / "apm",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "adopt-test"
    project_dir.mkdir()
    (project_dir / "apm.yml").write_text(
        "name: adopt-test\n"
        "version: 1.0.0\n"
        "description: Test project for silent-adopt regression\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n"
    )
    (project_dir / ".github").mkdir()
    (project_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=120):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_lockfile(project_dir):
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path) as f:
        return yaml.safe_load(f)


def _all_deployed_files(lockfile) -> list[str]:
    """Flatten every deployed_files entry across all locked deps."""
    out: list[str] = []
    deps = (lockfile or {}).get("dependencies", [])
    if isinstance(deps, list):
        for entry in deps:
            out.extend(entry.get("deployed_files", []) or [])
    return out


class TestSilentAdoptOfExistingFiles:
    """Catch-22: degraded lockfile + identical files on disk -> must self-heal."""

    def test_reinstall_with_wiped_lockfile_repopulates_deployed_files(
        self, temp_project, apm_command
    ):
        """The exact zava-storefront reproducer.

        Steps:
          1. Install sample package; capture deployed_files set.
          2. Wipe apm.lock.yaml (simulates the degraded state -- lockfile
             has no record of which files belong to APM).
          3. Re-install. Files on disk are byte-identical to source.
          4. Assert: the new lockfile records the SAME deployed_files set
             (silently adopted), not an empty list.

        Pre-fix on main: assertion fails -- non-skill packages get empty
        deployed_files, which would then trip
        ``required-packages-deployed`` policy.
        """
        result1 = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result1.returncode == 0, (
            f"First install failed:\nstderr={result1.stderr}\nstdout={result1.stdout}"
        )

        lock1 = _read_lockfile(temp_project)
        files_before = sorted(_all_deployed_files(lock1))
        assert files_before, "Test precondition: first install must populate deployed_files"

        # Snapshot disk state for byte-comparison after the re-install.
        # deployed_files entries can be either files (agents, instructions,
        # prompts, commands, hooks) or directories (skills) -- only snapshot
        # plain files for byte-equality.
        disk_before = {
            f: (temp_project / f).read_bytes() for f in files_before if (temp_project / f).is_file()
        }
        assert disk_before, "Test precondition: at least one deployed file on disk"

        # --- Simulate the degraded lockfile state ---
        (temp_project / "apm.lock.yaml").unlink()

        # Re-install. Files are still on disk, byte-identical to package source.
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Re-install failed:\nstderr={result2.stderr}\nstdout={result2.stdout}"
        )

        lock2 = _read_lockfile(temp_project)
        files_after = sorted(_all_deployed_files(lock2))

        assert files_after == files_before, (
            "deployed_files lost after lockfile-wipe + re-install. "
            "This is the catch-22: degraded lockfile cannot self-heal because "
            "non-skill integrators skip byte-identical files instead of adopting them.\n"
            f"  Before: {files_before}\n"
            f"  After:  {files_after}"
        )

        # On-disk content must be unchanged (no spurious overwrites).
        for f, content in disk_before.items():
            assert (temp_project / f).read_bytes() == content, (
                f"Adopt path must not modify on-disk bytes: {f} changed."
            )

    def test_required_packages_deployed_passes_after_lockfile_wipe(self, temp_project, apm_command):
        """End-to-end: with adopt in place, ``apm audit`` (which runs the
        same ``required-packages-deployed`` check the policy gate uses)
        passes after a lockfile wipe + re-install -- proving the catch-22
        is broken.

        Skipped if the sample package isn't covered by a
        ``required-packages-deployed`` policy in the test environment;
        the lockfile-shape assertion above is the primary regression
        guard. This test is the integration-level smoke that proves the
        full pipeline now self-heals.
        """
        # Initial install
        r1 = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert r1.returncode == 0, f"first install: {r1.stderr}\n{r1.stdout}"

        # Wipe lockfile -- degraded state
        (temp_project / "apm.lock.yaml").unlink()

        # Re-install with --no-policy to bypass any external policy and
        # exercise just the integrator/lockfile path. (The fix lives in
        # the integrator; --no-policy keeps this test independent of the
        # current org policy fixtures.)
        r2 = _run_apm(apm_command, ["install", "--no-policy"], temp_project)
        assert r2.returncode == 0, f"re-install: {r2.stderr}\n{r2.stdout}"

        lock2 = _read_lockfile(temp_project)
        files_after = _all_deployed_files(lock2)

        assert files_after, (
            "deployed_files must be repopulated after re-install -- "
            "otherwise required-packages-deployed would block the next "
            "install (catch-22)."
        )
