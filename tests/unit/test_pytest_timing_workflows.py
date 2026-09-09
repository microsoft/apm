"""Timing hints must not create inconsistent shard selection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

ROOT = Path(__file__).resolve().parents[2]
ACTION = "./.github/actions/pytest-timing"
RELEASE_INTEGRATION = "./.github/workflows/release-integration.yml"
SHARD_MATRIX = (
    "${{ fromJSON(inputs.integration-shard-count == 2 && '{\"shard\":[1,2]}' "
    "|| '{\"shard\":[1]}') }}"
)


def assert_shared_integration_snapshot(workflow: dict, integration: dict) -> None:
    build = workflow_job(workflow, "build")
    mq_candidate = "apm-mq-${{ github.run_attempt }}-linux-x86_64"
    assert workflow_step(build, "Upload binary")["with"]["name"] == mq_candidate
    snapshot = workflow_step(build, "Freeze integration scheduling hints")
    assert snapshot["uses"] == ACTION
    assert snapshot["with"] == {
        "suite": "integration",
        "snapshot": "integration-duration-snapshot-${{ github.run_attempt }}",
    }
    shards = workflow_job(workflow, "integration-tests-shard")
    assert "build" in shards["needs"]
    assert "smoke-test" not in shards["needs"]
    assert shards["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert shards["uses"] == RELEASE_INTEGRATION
    assert shards["with"]["candidate-artifact-name"] == mq_candidate
    assert shards["with"]["timing-snapshot-artifact"] == snapshot["with"]["snapshot"]
    assert shards["with"]["shard-count"] == 4
    assert shards["with"]["xdist-workers"] == 2
    assert "splitting-algorithm" not in shards["with"]
    assert shards["with"]["artifact-prefix"] == "mq-linux-${{ github.run_attempt }}"
    assert shards["with"]["runtime-prerequisites"] == "none"
    assert shards["with"]["run-network-integration-tests"] is True
    assert shards["with"]["coverage-enabled"] is True
    fan_in = workflow_job(workflow, "integration-tests")
    assert set(fan_in["needs"]) == {"integration-tests-shard", "scaling-guards", "smoke-test"}
    assert (
        workflow_step(workflow_job(workflow, "smoke-test"), "Download binary")["with"]["name"]
        == mq_candidate
    )
    assert (
        workflow_step(workflow_job(workflow, "release-validation"), "Download binary")["with"][
            "name"
        ]
        == mq_candidate
    )
    coverage_download = workflow_step(fan_in, "Download shard coverage")
    assert coverage_download["with"]["pattern"] == (
        "integration-coverage-mq-linux-${{ github.run_attempt }}-shard-*"
    )
    release_job = workflow_job(integration, "integration-tests")
    download = workflow_step(release_job, "Download shared integration scheduling snapshot")
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"name": "${{ inputs.timing-snapshot-artifact }}"}
    assert not any(step.get("uses") == ACTION for step in release_job["steps"])


def test_integration_shards_consume_one_immutable_history_snapshot() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/ci-integration.yml")
    integration = load_workflow(ROOT / ".github/workflows/release-integration.yml")
    assert_shared_integration_snapshot(workflow, integration)


@pytest.mark.parametrize("mutation", ["missing_dependency", "per_shard_snapshot", "rolling_cache"])
def test_snapshot_contract_rejects_divergent_shard_history(mutation: str) -> None:
    workflow = deepcopy(load_workflow(ROOT / ".github/workflows/ci-integration.yml"))
    integration = deepcopy(load_workflow(ROOT / ".github/workflows/release-integration.yml"))
    shards = workflow_job(workflow, "integration-tests-shard")
    if mutation == "missing_dependency":
        shards["needs"] = []
    elif mutation == "per_shard_snapshot":
        shards["with"]["timing-snapshot-artifact"] = "integration-duration-${{ matrix.shard }}"
    else:
        workflow_job(integration, "integration-tests")["steps"].append(
            {"uses": ACTION, "with": {"suite": "integration"}}
        )
    with pytest.raises(AssertionError):
        assert_shared_integration_snapshot(workflow, integration)


def assert_release_platform_snapshot(workflow: dict) -> None:
    build = workflow_job(workflow, "build")
    snapshot = workflow_step(build, "Freeze integration scheduling hints")
    assert snapshot["uses"] == ACTION
    assert snapshot["if"] == "inputs.full-validation && inputs.platform != 'windows'"
    assert snapshot["with"] == {
        "suite": "integration",
        "snapshot": "integration-duration-snapshot-${{ github.run_attempt }}-${{ inputs.binary-name }}",
    }
    job = workflow_job(workflow, "integration-tests-shard")
    assert job["uses"] == RELEASE_INTEGRATION
    assert job["needs"] == ["build"]
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"] == SHARD_MATRIX
    assert job["with"]["timing-snapshot-artifact"] == snapshot["with"]["snapshot"]
    assert job["with"]["shard-count"] == "${{ inputs.integration-shard-count }}"
    assert job["with"]["shard-index"] == "${{ matrix.shard }}"
    assert job["with"]["xdist-workers"] == "${{ inputs.integration-xdist-workers }}"
    assert job["with"]["splitting-algorithm"] == "${{ inputs.integration-splitting-algorithm }}"
    assert job["with"]["artifact-prefix"] == "${{ github.run_attempt }}-${{ inputs.binary-name }}"
    assert job["with"]["runtime-prerequisites"] == "none"


def test_release_platform_freezes_one_snapshot_before_native_shards() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/release-platform.yml")
    assert_release_platform_snapshot(workflow)


def test_release_platform_snapshot_mutation_is_rejected() -> None:
    workflow = deepcopy(load_workflow(ROOT / ".github/workflows/release-platform.yml"))
    workflow_job(workflow, "integration-tests-shard")["with"]["timing-snapshot-artifact"] = (
        "integration-duration-${{ matrix.shard }}"
    )
    with pytest.raises(AssertionError):
        assert_release_platform_snapshot(workflow)


def test_unit_shards_record_timings_without_a_mutable_selection_cache() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
    shards = workflow_job(workflow, "build-and-test-shard")
    assert not shards.get("needs")
    assert shards["strategy"]["matrix"]["shard"] == [1, 2]
    assert not any(step.get("uses") == ACTION for step in shards["steps"])
    command = workflow_step(shards, "Run tests with coverage")["run"]
    assert "--store-durations" in command
    assert "--junitxml=test-results/unit.xml" in command


def test_snapshot_publication_is_complete_and_never_writes_the_rolling_cache() -> None:
    action = load_workflow(ROOT / ".github/actions/pytest-timing/action.yml")
    snapshot_steps = [
        step for step in action["runs"]["steps"] if step.get("if") == "inputs.snapshot != ''"
    ]
    assert any(step.get("uses", "").startswith("actions/cache/restore@") for step in snapshot_steps)
    assert not any(
        step.get("uses", "").startswith(("actions/cache@", "actions/cache/save@"))
        for step in snapshot_steps
    )
    upload = next(
        step
        for step in snapshot_steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"] == ".test_durations"


@pytest.mark.parametrize("weighted", [False, True])
def test_real_split_and_xdist_preserve_complete_disjoint_coverage(
    tmp_path: Path, weighted: bool
) -> None:
    """Execute both shards with the deployed plugins and one immutable input."""
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\nmarkers = xdist_group(name): fixture affinity\n", encoding="ascii")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n"
        "@pytest.mark.xdist_group('shared_home')\n"
        "@pytest.mark.parametrize('index', range(8))\n"
        "def test_probe(index):\n"
        "    assert 0 <= index < 8\n",
        encoding="ascii",
    )
    expected = {f"test_probe.py::test_probe[{index}]" for index in range(8)}
    hints = {node: (20.0 if "[0]" in node else 1.0) for node in expected} if weighted else {}
    hints["test_removed.py::test_old"] = 200.0
    snapshot = json.dumps(hints)
    observed: list[set[str]] = []
    for shard in (1, 2):
        durations = tmp_path / f"durations-{shard}.json"
        durations.write_text(snapshot, encoding="ascii")
        outcomes = tmp_path / f"outcomes-{shard}.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(config),
                "--confcutdir",
                str(tmp_path),
                str(probe),
                "-q",
                "--splits",
                "2",
                "--group",
                str(shard),
                "--splitting-algorithm",
                "least_duration",
                "-n",
                "2",
                "--dist",
                "loadgroup",
                "--store-durations",
                "--durations-path",
                str(durations),
                f"--junitxml={outcomes}",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTEST_ADDOPTS": ""},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        cases = list(ET.parse(outcomes).iter("testcase"))  # noqa: S314 - generated by this probe.
        assert cases
        assert all(case.attrib["classname"] == "test_probe" for case in cases)
        nodes = {f"test_probe.py::{case.attrib['name']}" for case in cases}
        observed.append(nodes)
        stored = json.loads(durations.read_text("ascii"))
        assert stored
        assert all(duration >= 0 for duration in stored.values())
    assert observed[0].isdisjoint(observed[1])
    assert observed[0] | observed[1] == expected
