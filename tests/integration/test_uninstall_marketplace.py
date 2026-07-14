"""End-to-end integration test for ``apm uninstall`` with marketplace notation.

Seeds a minimal project (``apm.yml`` + ``apm.lock.yaml`` + a fake ``apm_modules``
directory) so no network calls are required, then exercises the
``apm uninstall name@marketplace`` path end-to-end with the real apm binary.

Does **not** require a GitHub token -- the lockfile is pre-seeded so the
offline Stage 1 resolution path is exercised exclusively.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.requires_apm_binary]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def apm_command(apm_binary_path: Path) -> str:
    return str(apm_binary_path)


def _run_apm(apm_command, args, cwd, timeout=60):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _seed_project(project_dir: Path, canonical: str, marketplace_name: str, plugin_name: str):
    """Create a minimal pre-seeded project with a fake installed package."""
    owner, repo = canonical.split("/", 1)

    # apm.yml
    config = {
        "name": "uninstall-marketplace-test",
        "version": "1.0.0",
        "dependencies": {"apm": [canonical], "mcp": []},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )

    # Fake apm_modules directory
    pkg_dir = project_dir / "apm_modules" / owner / repo
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "apm.yml").write_text(
        yaml.dump({"name": repo, "version": "1.0.0"}), encoding="utf-8"
    )

    # apm.lock.yaml pre-seeded with the lockfile entry (list format)
    lockfile = {
        "lockfile_version": "1",
        "dependencies": [
            {
                "repo_url": canonical,
                "resolved_commit": "abc1234567890abc1234567890abc1234567890ab",
                "discovered_via": marketplace_name,
                "marketplace_plugin_name": plugin_name,
                "deployed_files": [],
            }
        ],
    }
    (project_dir / "apm.lock.yaml").write_text(
        yaml.dump(lockfile, default_flow_style=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_uninstall_marketplace_notation_via_lockfile(apm_command, tmp_path):
    """``apm uninstall name@marketplace`` resolves via lockfile and removes the package."""
    project = tmp_path / "project"
    project.mkdir()

    canonical = "owner/my-plugin"
    marketplace_name = "official"
    plugin_name = "my-plugin"
    _seed_project(project, canonical, marketplace_name, plugin_name)

    result = _run_apm(
        apm_command,
        ["uninstall", f"{plugin_name}@{marketplace_name}"],
        project,
    )

    assert result.returncode == 0, (
        f"apm uninstall failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    # Package must be removed from apm.yml
    data = yaml.safe_load((project / "apm.yml").read_text())
    assert canonical not in (data.get("dependencies", {}).get("apm") or []), (
        f"Expected {canonical!r} to be removed from apm.yml"
    )

    # apm_modules directory must be gone (or lockfile deleted when no deps remain)
    pkg_dir = project / "apm_modules" / "owner" / "my-plugin"
    assert not pkg_dir.exists(), f"Expected {pkg_dir} to be removed"


def test_uninstall_marketplace_dry_run_no_changes(apm_command, tmp_path):
    """``apm uninstall name@marketplace --dry-run`` resolves but does not mutate disk."""
    project = tmp_path / "project"
    project.mkdir()

    canonical = "owner/my-plugin"
    _seed_project(project, canonical, "official", "my-plugin")

    result = _run_apm(
        apm_command,
        ["uninstall", "my-plugin@official", "--dry-run"],
        project,
    )

    assert result.returncode == 0, (
        f"apm uninstall --dry-run failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    # apm.yml must be unchanged
    data = yaml.safe_load((project / "apm.yml").read_text())
    assert canonical in (data.get("dependencies", {}).get("apm") or []), (
        f"Expected {canonical!r} to remain in apm.yml after dry-run"
    )

    # apm_modules must still exist
    pkg_dir = project / "apm_modules" / "owner" / "my-plugin"
    assert pkg_dir.exists(), "Expected apm_modules to be untouched after dry-run"


def test_uninstall_marketplace_dry_run_no_lockfile_warns(apm_command, tmp_path):
    """``apm uninstall name@marketplace --dry-run`` without a lockfile warns and skips.

    Stage 1 (lockfile lookup) finds nothing because no lockfile exists, and Stage 2
    (registry fallback) is skipped under ``--dry-run``. The CLI must surface a
    user-visible warning that the ref could not be resolved in dry-run mode,
    rather than silently exiting or emitting a misleading error.
    """
    project = tmp_path / "project"
    project.mkdir()

    canonical = "owner/my-plugin"
    config = {
        "name": "uninstall-no-lockfile-test",
        "version": "1.0.0",
        "dependencies": {"apm": [canonical], "mcp": []},
    }
    (project / "apm.yml").write_text(yaml.dump(config, default_flow_style=False), encoding="utf-8")
    # Deliberately do NOT seed apm.lock.yaml -- this is the no-lockfile path.

    result = _run_apm(
        apm_command,
        ["uninstall", "my-plugin@official", "--dry-run"],
        project,
    )

    combined = result.stdout + result.stderr
    assert "could not be resolved" in combined or "dry-run" in combined.lower(), (
        f"Expected dry-run resolution warning in output. "
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # apm.yml must be untouched regardless of the warning.
    data = yaml.safe_load((project / "apm.yml").read_text())
    assert canonical in (data.get("dependencies", {}).get("apm") or []), (
        "Expected apm.yml unchanged after dry-run"
    )
