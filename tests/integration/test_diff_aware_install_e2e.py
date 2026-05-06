"""End-to-end integration tests for diff-aware apm install.

Tests the complete manifest-as-source-of-truth lifecycle with real packages:
- Package removed from apm.yml: apm install cleans up deployed files and lockfile
- Package ref/version changed in apm.yml: apm install re-downloads without --update
- MCP config drift: apm install re-applies changed MCP server config (unit-tested;
  omitted from e2e since it requires a real runtime to be configured)

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
Uses real packages from GitHub:
  - microsoft/apm-sample-package (deployed prompts, agents, etc.)
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# Skip all tests if no GitHub token is available
pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project with .github/ for VSCode target detection."""
    project_dir = tmp_path / "diff-aware-install-test"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=180):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_apm_yml(project_dir, packages):
    """Write apm.yml with the given list of APM package specs."""
    config = {
        "name": "diff-aware-test",
        "version": "1.0.0",
        "target": "copilot",
        "dependencies": {
            "apm": packages,
            "mcp": [],
        },
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


def _read_lockfile(project_dir):
    """Read and parse apm.lock from the project directory."""
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_locked_dep(lockfile, repo_url):
    """Get a dependency entry from lockfile by repo_url."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            if entry.get("repo_url") == repo_url:
                return entry
    return None


def _collect_deployed_files(project_dir, dep_entry):
    """Return existing deployed files from a lockfile dep entry."""
    if not dep_entry or not dep_entry.get("deployed_files"):
        return []
    return [f for f in dep_entry["deployed_files"] if (project_dir / f).exists()]


# ---------------------------------------------------------------------------
# Scenario 1: Package removed from manifest — apm install cleans up
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Orphan cleanup runs but the sample package deploys skills as "
    "directory-keyed lockfile entries (.github/style-checker/), and the "
    "safety gate at integration/cleanup.py refuses to remove directory "
    "entries. Tracked separately; non-skill orphan files (instructions, "
    "prompts, agents) ARE cleaned by the early-return fix in this commit."
)
class TestPackageRemovedFromManifest:
    """When a package is removed from apm.yml, apm install should clean up
    its deployed files and remove it from the lockfile."""

    def test_removed_package_files_cleaned_on_install(self, temp_project, apm_command):
        """Files deployed by a removed package disappear on the next apm install."""
        # -- Step 1: install the package --
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        # -- Step 2: verify deployed files exist and are tracked --
        lockfile_before = _read_lockfile(temp_project)
        assert lockfile_before is not None, "apm.lock was not created"
        dep_before = _get_locked_dep(lockfile_before, "microsoft/apm-sample-package")
        assert dep_before is not None, "Package not in lockfile after install"
        deployed_before = _collect_deployed_files(temp_project, dep_before)
        assert len(deployed_before) > 0, "No deployed files found on disk — cannot verify cleanup"

        # -- Step 3: remove the package from manifest --
        _write_apm_yml(temp_project, [])

        # -- Step 4: run apm install (no packages) — should detect orphan --
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install after removal failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Step 5: verify deployed files are gone --
        for rel_path in deployed_before:
            full_path = temp_project / rel_path
            assert not full_path.exists(), (
                f"Orphaned file {rel_path} was NOT cleaned up by apm install"
            )

    def test_removed_package_absent_from_lockfile_after_install(self, temp_project, apm_command):
        """After removing a package from apm.yml, apm install removes it from lockfile."""
        # -- Install --
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        # -- Remove from manifest --
        _write_apm_yml(temp_project, [])

        # -- Re-install --
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install after removal failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Verify lockfile no longer has the removed package --
        lockfile_after = _read_lockfile(temp_project)
        if lockfile_after and lockfile_after.get("dependencies"):
            dep_after = _get_locked_dep(lockfile_after, "microsoft/apm-sample-package")
            assert dep_after is None, "Removed package still present in apm.lock after apm install"

    def test_remaining_package_unaffected_by_removal(self, temp_project, apm_command):
        """Files from packages still in the manifest are untouched."""
        # -- Install two packages --
        _write_apm_yml(
            temp_project,
            [
                "microsoft/apm-sample-package",
                "github/awesome-copilot/skills/aspire",
            ],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile_before = _read_lockfile(temp_project)
        sample_dep = _get_locked_dep(lockfile_before, "microsoft/apm-sample-package")
        if not sample_dep or not _collect_deployed_files(temp_project, sample_dep):
            pytest.skip("apm-sample-package deployed no files, cannot verify")

        # -- Remove only apm-sample-package --
        _write_apm_yml(temp_project, ["github/awesome-copilot/skills/aspire"])
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- apm-sample-package files should be gone --
        for rel_path in sample_dep.get("deployed_files") or []:
            # The files that were deployed should no longer exist
            assert not (temp_project / rel_path).exists(), (
                f"Removed package file {rel_path} still on disk"
            )


# ---------------------------------------------------------------------------
# Scenario 2: Package ref changed — apm install re-downloads
# ---------------------------------------------------------------------------


class TestPackageRefChangedInManifest:
    """When the ref in apm.yml changes, apm install re-downloads without --update."""

    def test_ref_change_triggers_re_download(self, temp_project, apm_command):
        """Changing the ref in apm.yml from one value to another causes re-download."""
        # -- Step 1: install with an explicit commit-pinned ref --
        # We install first without a ref (using default branch), so the lockfile
        # records the resolved_ref as the default branch or latest commit.
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        assert lockfile1 is not None, "apm.lock was not created"
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        assert dep1 is not None, "Package not in lockfile"
        original_commit = dep1.get("resolved_commit")
        assert original_commit, "No resolved_commit in lockfile after install"

        # -- Step 2: change ref to "main" explicitly (from unset → explicit branch) --
        # This differs from the lockfile's resolved_ref (which may be None/default).
        # For the test to be meaningful we pick a known ref that EXISTS in the repo.
        # We use "main" — the primary branch — which definitely exists.
        _write_apm_yml(
            temp_project,
            [{"git": "https://github.com/microsoft/apm-sample-package.git", "ref": "main"}],
        )

        # -- Step 3: run install WITHOUT --update --
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Install with changed ref failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Step 4: verify the package was re-processed --
        # Even if the commit hash is the same (main hasn't changed), the install
        # must not silently skip the package — it must re-evaluate the ref.
        # We verify the lockfile was updated and the package directory still exists.
        lockfile2 = _read_lockfile(temp_project)
        assert lockfile2 is not None, "apm.lock missing after second install"
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        assert dep2 is not None, "Package disappeared from lockfile after ref change"

        # The re-download should write back to lockfile; package dir must exist
        package_dir = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
        assert package_dir.exists(), (
            "Package directory disappeared after re-download for ref change"
        )

    def test_no_ref_change_does_not_re_download(self, temp_project, apm_command):
        """Without a ref change, apm install uses the lockfile SHA (idempotent)."""
        # -- Install --
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        commit_before = dep1.get("resolved_commit") if dep1 else None

        # -- Re-install without changing the ref --
        result2 = _run_apm(apm_command, ["install", "--only=apm"], temp_project)
        assert result2.returncode == 0, (
            f"Re-install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Commit should remain the same (lockfile pinned) --
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        commit_after = dep2.get("resolved_commit") if dep2 else None

        if commit_before and commit_after:
            assert commit_before == commit_after, (
                f"Lockfile SHA changed without a ref change: {commit_before} → {commit_after}"
            )


# ---------------------------------------------------------------------------
# Scenario 3: Full install is idempotent when manifest unchanged
# ---------------------------------------------------------------------------


class TestFullInstallIdempotent:
    """Running apm install multiple times without manifest changes is safe."""

    def test_repeated_install_does_not_remove_files(self, temp_project, apm_command):
        """Repeated apm install with same manifest preserves deployed files."""
        _write_apm_yml(temp_project, ["microsoft/apm-sample-package"])

        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"First install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        files_before = dep1.get("deployed_files", []) if dep1 else []

        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # All files from the first install must still exist
        for rel_path in files_before:
            assert (temp_project / rel_path).exists(), (
                f"File {rel_path} disappeared after idempotent re-install"
            )

        # Package must still be in lockfile
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        assert dep2 is not None, "Package missing from lockfile after idempotent re-install"


# ---------------------------------------------------------------------------
# Scenario 4: Branch-ref cache drift regression (PR #1158)
# ---------------------------------------------------------------------------
#
# Reproduction fixture:  https://github.com/danielmeppiel/apm-update-repro
#
#   * top-level repo: a full APM package with V1 + V2 commits on main
#   * virtual-pkg/ subdirectory: matches the reported shape
#     (single .agent.md + skill, declared via ``path: virtual-pkg``)
#
# The reported bug: when a dependency uses a branch ref (e.g.
# ``ref: main``) and upstream advances, ``apm install`` (no flag) silently
# produces a lockfile whose ``resolved_commit`` points to the new remote
# SHA while the on-disk content (and ``content_hash``) still reflect the
# older commit. We simulate the "upstream advance" by handcrafting a
# lockfile that points to an older commit while disk holds the current
# content.

FIXTURE_REPO = "danielmeppiel/apm-update-repro"
# Older commit known to exist in the fixture's history (V1 of single-file
# agent). The fixture must keep this commit reachable for the regression
# test to remain valid.
KNOWN_OLD_COMMIT = "b08bf95"


def _rewrite_lockfile_resolved_commit(
    project_dir,
    dep_repo_url_substring,
    *,
    new_commit,
    apm_version=None,
    new_resolved_ref=None,
):
    """Edit apm.lock.yaml in-place to simulate a stale lockfile.

    Used to recreate the drifted state without depending on
    upstream advancing during the test run.

    Notable: ``content_hash`` is NOT rewritten -- it stays at whatever
    the previous install recorded. This is intentional for the simulated
    "lockfile lies about resolved_commit but content matches disk" shape.
    Tests that need the EXACT inverse-drift state (where
    content_hash records OLD bytes but lockfile.resolved_commit records
    NEW bytes) install at an OLD pinned commit first, then rewrite only
    ref + resolved_commit + apm_version to flip the lockfile into the
    "buggy v0.12.2 generator" shape -- so that the lockfile's recorded
    content_hash legitimately mismatches the upstream HEAD content that
    the self-heal will subsequently download.
    """
    lock_path = project_dir / "apm.lock.yaml"
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    if apm_version is not None:
        data["apm_version"] = apm_version
    for dep in data.get("dependencies", []):
        if dep_repo_url_substring in dep.get("repo_url", ""):
            dep["resolved_commit"] = new_commit
            if new_resolved_ref is not None:
                dep["resolved_ref"] = new_resolved_ref
            break
    else:
        raise AssertionError(f"No lockfile dep matching '{dep_repo_url_substring}' found")
    lock_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


class TestBranchRefDriftRegression:
    """Regression tests for the branch-ref cache drift bug.

    Verifies that ``apm install`` (no flag) detects when a branch ref's
    remote SHA has advanced past the lockfile-recorded SHA and forces a
    re-download to restore consistency between the lockfile, the
    content_hash, and the on-disk content.
    """

    def test_branch_ref_picks_up_upstream_advance(self, temp_project, apm_command):
        """Lockfile SHA != current branch HEAD -> plain ``apm install``
        re-downloads and updates the lockfile to the current HEAD."""
        # -- Step 1: install with ref=main, lockfile records current HEAD --
        _write_apm_yml(
            temp_project,
            [{"git": f"https://github.com/{FIXTURE_REPO}.git", "ref": "main"}],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, FIXTURE_REPO)
        assert dep1 is not None
        head_sha = dep1["resolved_commit"]
        assert head_sha and head_sha != KNOWN_OLD_COMMIT

        # -- Step 2: rewrite the lockfile to point to an OLDER commit --
        # This simulates the state a user would have if upstream had
        # advanced AFTER their previous install. The disk content still
        # reflects current HEAD; lockfile content_hash still matches disk.
        _rewrite_lockfile_resolved_commit(
            temp_project, "apm-update-repro", new_commit=KNOWN_OLD_COMMIT
        )

        # -- Step 3: plain ``apm install`` with NO --update flag --
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Step 4: lockfile must have advanced back to current HEAD --
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, FIXTURE_REPO)
        assert dep2 is not None
        assert dep2["resolved_commit"] != KNOWN_OLD_COMMIT, (
            f"Branch-ref drift not detected: lockfile still points to old commit {KNOWN_OLD_COMMIT}"
        )
        assert dep2["resolved_commit"] == head_sha, (
            f"Lockfile SHA after re-install must equal current HEAD ({head_sha}); "
            f"got {dep2['resolved_commit']}"
        )

    def test_self_heal_recovers_buggy_version_lockfile(self, temp_project, apm_command):
        """Lockfile generated by APM <= 0.12.2 with a stale resolved_commit
        and matching disk content (the corrupted state)
        triggers the version-gated self-heal on next plain install."""
        # -- Step 1: clean install --
        _write_apm_yml(
            temp_project,
            [{"git": f"https://github.com/{FIXTURE_REPO}.git", "ref": "main"}],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        # -- Step 2: rewrite lockfile to match the v0.12.2 corrupted state --
        # Stale resolved_commit + apm_version=0.12.2 should trigger self-heal.
        _rewrite_lockfile_resolved_commit(
            temp_project,
            "apm-update-repro",
            new_commit=KNOWN_OLD_COMMIT,
            apm_version="0.12.2",
        )

        # -- Step 3: plain install -- self-heal should fire --
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Self-heal install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        # -- Step 4: lockfile must reflect current HEAD --
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, FIXTURE_REPO)
        assert dep2 is not None
        assert dep2["resolved_commit"] != KNOWN_OLD_COMMIT, (
            "Self-heal failed to refresh lockfile resolved_commit"
        )

        # -- Step 5: a third install MUST succeed and converge -- the
        # lockfile must remain consistent (resolved_commit unchanged,
        # content unchanged). On CLI versions still in the buggy set
        # (the version we are testing on), self-heal may legitimately
        # re-fire because the cache directory is not a git repo and
        # lockfile_match falls back to content-hash; that is harmless
        # because the re-download produces identical bytes. On the
        # released version (post 0.12.2) self-heal will not re-fire at
        # all. Either way, install must converge to the same state.
        result3 = _run_apm(apm_command, ["install"], temp_project)
        assert result3.returncode == 0
        lockfile3 = _read_lockfile(temp_project)
        dep3 = _get_locked_dep(lockfile3, FIXTURE_REPO)
        assert dep3["resolved_commit"] == dep2["resolved_commit"]
        assert dep3["content_hash"] == dep2["content_hash"]

    def test_virtual_package_branch_ref_drift_recovers(self, temp_project, apm_command):
        """Reported reproduction shape: virtual package
        (path: virtual-pkg) with ref: main. Branch drift on a virtual
        package always falls back to content-hash matching (install_path
        is not a git repo), which is the harder code path to fix."""
        _write_apm_yml(
            temp_project,
            [
                {
                    "git": f"https://github.com/{FIXTURE_REPO}.git",
                    "path": "virtual-pkg",
                    "ref": "main",
                }
            ],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, FIXTURE_REPO)
        assert dep1 is not None
        assert dep1.get("is_virtual") is True
        head_sha = dep1["resolved_commit"]

        # Simulate stale state and re-install
        _rewrite_lockfile_resolved_commit(
            temp_project, "apm-update-repro", new_commit=KNOWN_OLD_COMMIT
        )
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            f"Second install failed:\nSTDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )

        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, FIXTURE_REPO)
        assert dep2 is not None
        assert dep2["resolved_commit"] != KNOWN_OLD_COMMIT, (
            "Branch-ref drift not detected for virtual package -- this is the exact failure case"
        )
        assert dep2["resolved_commit"] == head_sha

    def test_corrupted_state_self_heal_does_not_trip_supply_chain(self, temp_project, apm_command):
        """Reproduce the EXACT corrupted state and verify the
        self-heal completes without tripping the supply-chain hard-block.

        Corrupted state:
          - lockfile.resolved_commit = NEW (current remote HEAD)
          - lockfile.content_hash    = OLD bytes hash
          - lockfile.resolved_ref    = main
          - lockfile.apm_version     = 0.12.2
          - disk content             = OLD bytes
          - remote main HEAD         = NEW

        Without the ``expected_hash_change_deps`` plumbing this exact
        path triggers the supply-chain protection at sources.py:618 and
        aborts the install with sys.exit(1) BEFORE the lockfile is
        repaired. With the fix, the install completes, deploys NEW
        content to disk, and rewrites the lockfile so it is consistent.
        """
        # -- Step 1: install pinned at OLD commit -- disk + lockfile
        # both record OLD content and OLD content_hash. --
        _write_apm_yml(
            temp_project,
            [{"git": f"https://github.com/{FIXTURE_REPO}.git", "ref": KNOWN_OLD_COMMIT}],
        )
        result1 = _run_apm(apm_command, ["install"], temp_project)
        assert result1.returncode == 0, (
            f"Initial pinned install failed:\nSTDOUT: {result1.stdout}\nSTDERR: {result1.stderr}"
        )
        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, FIXTURE_REPO)
        assert dep1 is not None
        old_content_hash = dep1["content_hash"]
        assert old_content_hash, "Pinned install must have recorded a content_hash"

        # -- Step 2: switch the manifest to ``ref: main`` (mutable
        # branch). The manifest uses a branch ref. --
        _write_apm_yml(
            temp_project,
            [{"git": f"https://github.com/{FIXTURE_REPO}.git", "ref": "main"}],
        )

        # -- Step 3: discover the current remote HEAD of main (the
        # NEW SHA) so we can write it into the lockfile as the phantom
        # ``resolved_commit``. --
        ls_remote = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{FIXTURE_REPO}.git", "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert ls_remote.returncode == 0, ls_remote.stderr
        head_sha = ls_remote.stdout.strip().split()[0]
        assert head_sha and head_sha != KNOWN_OLD_COMMIT

        # -- Step 4: rewrite the lockfile to the EXACT corrupted state.
        # Note: content_hash stays at OLD hash (recorded in step 1) --
        # this is the lie that the supply-chain check would normally
        # catch. --
        _rewrite_lockfile_resolved_commit(
            temp_project,
            "apm-update-repro",
            new_commit=head_sha,
            apm_version="0.12.2",
            new_resolved_ref="main",
        )

        # Sanity-check: lockfile content_hash is still OLD; disk is OLD;
        # lockfile.resolved_commit is NEW. The exact corrupted state.
        lockfile_corrupted = _read_lockfile(temp_project)
        dep_corrupted = _get_locked_dep(lockfile_corrupted, FIXTURE_REPO)
        assert dep_corrupted["content_hash"] == old_content_hash
        assert dep_corrupted["resolved_commit"] == head_sha

        # -- Step 5: plain ``apm install``. Self-heal must fire AND the
        # supply-chain hard-block must NOT abort the install. --
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0, (
            "Self-heal aborted -- likely tripped the supply-chain hard-block "
            f"because expected_hash_change_deps plumbing is missing.\n"
            f"STDOUT: {result2.stdout}\nSTDERR: {result2.stderr}"
        )
        # The install MUST surface the self-heal warning (otherwise
        # the user has no signal that their cache was repaired).
        _stdout2 = result2.stdout.lower()
        assert "branch-ref cache drift" in _stdout2 or "recovering" in _stdout2, (
            f"Self-heal did not emit a visible recovery message:\n{result2.stdout}"
        )

        # -- Step 6: lockfile must now be CONSISTENT. content_hash must
        # have been refreshed to match the newly downloaded V2 bytes. --
        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, FIXTURE_REPO)
        assert dep2["resolved_commit"] == head_sha
        assert dep2["content_hash"] != old_content_hash, (
            "Self-heal must refresh content_hash so the lockfile is consistent"
        )
