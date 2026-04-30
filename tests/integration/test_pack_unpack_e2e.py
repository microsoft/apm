"""End-to-end integration tests for ``apm pack`` and ``apm unpack``.

Round-trip tests: install → pack → unpack → verify files match.

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
"""

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
    """Create a temporary APM project with a dependency on apm-sample-package."""
    project_dir = tmp_path / "pack-test"
    project_dir.mkdir()

    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text(
        "name: pack-test\n"
        "version: 1.0.0\n"
        "description: Test project for pack/unpack\n"
        "dependencies:\n"
        "  apm:\n"
        "    - microsoft/apm-sample-package\n"
    )

    (project_dir / ".github").mkdir()
    return project_dir


def _run_apm(apm_command, args, cwd, timeout=120):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestPackUnpackE2E:
    def test_full_round_trip(self, apm_command, temp_project, tmp_path):
        """Install → pack --archive → unpack in fresh dir → files match."""
        # 1. Install
        result = _run_apm(apm_command, ["install"], cwd=temp_project)
        assert result.returncode == 0, f"install failed: {result.stderr}"
        assert (temp_project / "apm.lock.yaml").exists()

        # 2. Pack
        result = _run_apm(apm_command, ["pack", "--archive"], cwd=temp_project)
        assert result.returncode == 0, f"pack failed: {result.stderr}"

        build_dir = temp_project / "build"
        archives = list(build_dir.glob("*.tar.gz"))
        assert len(archives) == 1, f"Expected 1 archive, found {archives}"

        # 3. Unpack in a clean directory
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        archive = archives[0]

        result = _run_apm(apm_command, ["unpack", str(archive)], cwd=consumer)
        assert result.returncode == 0, f"unpack failed: {result.stderr}"

        # 4. Verify .github/ files are present
        assert (consumer / ".github").exists(), ".github/ missing after unpack"

    def test_pack_dry_run_no_side_effects(self, apm_command, temp_project):
        """Dry run should not create any files."""
        result = _run_apm(apm_command, ["install"], cwd=temp_project)
        assert result.returncode == 0

        result = _run_apm(apm_command, ["pack", "--dry-run"], cwd=temp_project)
        assert result.returncode == 0

        assert not (temp_project / "build").exists()
