"""Regression tests for release-validation assets in build-release.yml."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-release.yml"
CANONICAL_HELPER = "./src/apm_cli/runtime/scripts/github-token-helper.sh"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_release_artifacts_upload_the_canonical_token_helper():
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "./scripts/github-token-helper.sh" not in workflow_text

    jobs = _workflow()["jobs"]
    artifact_jobs = (
        "build-and-test",
        "build-and-validate-macos-intel",
        "build-and-validate-macos-arm",
    )
    for job_name in artifact_jobs:
        paths = _step(jobs[job_name], "Upload binary as workflow artifact")["with"]["path"]
        assert CANONICAL_HELPER in paths.splitlines()


@pytest.mark.parametrize(
    ("job_name", "binary_name"),
    (
        ("build-and-validate-macos-intel", "apm-darwin-x86_64"),
        ("build-and-validate-macos-arm", "apm-darwin-arm64"),
    ),
)
@pytest.mark.skipif(sys.platform == "win32", reason="workflow prepare blocks require POSIX tools")
def test_macos_prepare_block_preserves_script_layout(tmp_path, job_name, binary_name):
    job = _workflow()["jobs"][job_name]
    prepare_script = _step(job, "Prepare isolated release-validation environment")["run"]
    isolated_dir = tmp_path / "apm-isolated-test"
    prepare_script = prepare_script.replace("/tmp/apm-isolated-test", str(isolated_dir))

    workspace = tmp_path / "workspace"
    scripts_dir = workspace / "scripts"
    helper_dir = workspace / "src" / "apm_cli" / "runtime" / "scripts"
    scripts_dir.mkdir(parents=True)
    helper_dir.mkdir(parents=True)
    for script_name in ("test-release-validation.sh", "test-dependency-integration.sh"):
        shutil.copy2(REPO_ROOT / "scripts" / script_name, scripts_dir / script_name)
    shutil.copy2(
        REPO_ROOT / CANONICAL_HELPER.removeprefix("./"), helper_dir / "github-token-helper.sh"
    )

    binary = workspace / "dist" / binary_name / "apm"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    github_path = tmp_path / "github-path"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "GITHUB_PATH": str(github_path),
        "HOME": str(home),
        "PATH": os.environ["PATH"],
    }

    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", prepare_script],
        cwd=workspace,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    validation = isolated_dir / "scripts" / "test-release-validation.sh"
    dependency = isolated_dir / "scripts" / "test-dependency-integration.sh"
    helper = isolated_dir / "src" / "apm_cli" / "runtime" / "scripts" / "github-token-helper.sh"
    assert validation.is_file()
    assert dependency.is_file()
    assert helper.is_file()
    assert github_path.read_text(encoding="utf-8").strip() == str(isolated_dir)

    preamble = validation.with_name("release-validation-preamble.sh")
    validation_text = validation.read_text(encoding="utf-8")
    main_marker = "# Run main function"
    assert main_marker in validation_text
    preamble.write_text(validation_text.split(main_marker, maxsplit=1)[0], encoding="utf-8")
    sourced = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            f'source "{preamble}"; declare -F setup_github_tokens test_real_dependency_installation',
        ],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert "Setting up GitHub tokens" not in sourced.stdout
    assert sourced.stdout.splitlines()[-2:] == [
        "setup_github_tokens",
        "test_real_dependency_installation",
    ]

    run_script = _step(job, "Run release validation tests")["run"]
    assert "./scripts/test-release-validation.sh" in run_script
