"""Bind APM's local receipt writes to the existing frozen no-rewrite contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_manifest_version_snapshot_lifecycle import (
    _branch_switch,
    _clear_installed_fixture,
)
from tests.integration.test_skill_materialization_receipt_lifecycle import _receipt

pytestmark = pytest.mark.e2e


@pytest.fixture
def installed_entrypoint() -> Path:
    """Use the package installed by the conformance job's uv run, never skip."""
    binary = Path(sys.executable).parent / ("apm.exe" if os.name == "nt" else "apm")
    assert binary.is_file(), (
        f"The active Python environment must install the APM entrypoint: {binary}"
    )
    return binary


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize("source", ["local", "git-subdirectory"])
@pytest.mark.parametrize("create", [False, True], ids=["update-receipt", "create-receipt"])
def test_frozen_receipt_write_never_rewrites_committed_lock(
    tmp_path: Path, installed_entrypoint: Path, source: str, create: bool
) -> None:
    """Successful materialization may record local evidence, not rewrite the lock."""
    scenario = _branch_switch(tmp_path, installed_entrypoint, source)
    receipt = _receipt(scenario)
    prior_receipt = receipt.read_bytes()
    if create:
        _clear_installed_fixture(scenario.root)
        receipt.unlink()
    manifest = scenario.root / "apm.yml"
    lockfile = scenario.root / "apm.lock.yaml"
    committed = (manifest.read_bytes(), lockfile.read_bytes())
    locked_stat = lockfile.stat()
    pins = load_yaml(lockfile)["dependencies"]

    for _ in range(2):
        result = scenario.install("--frozen")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (manifest.read_bytes(), lockfile.read_bytes()) == committed
        current_stat = lockfile.stat()
        assert (current_stat.st_ino, current_stat.st_mtime_ns) == (
            locked_stat.st_ino,
            locked_stat.st_mtime_ns,
        )
        assert load_yaml(lockfile)["dependencies"] == pins
        deployed = scenario.deployed_skill.read_bytes()
        assert deployed.endswith(b"Body 1.1.0.\n")
        proof = json.loads(receipt.read_bytes())
        assert proof["root"] == scenario.root.resolve().as_posix()
        assert proof["scope"] == "project"
        assert len(proof["records"]) == 1
        record = proof["records"][0]
        assert record["value"] == ".claude/skills/demo/SKILL.md"
        assert record["content_hash"] == "sha256:" + hashlib.sha256(deployed).hexdigest()
        assert receipt.read_bytes() != prior_receipt
        if scenario.commit:
            assert pins[0]["resolved_commit"] == scenario.commit
            assert pins[0]["resolved_ref"] == "release-new"


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize("existing_receipt", [False, True], ids=["absent-receipt", "prior-receipt"])
@pytest.mark.parametrize("invalid", ["missing-lock", "mcp-mismatch"])
def test_frozen_admission_failure_cannot_promote_materialization_receipt(
    tmp_path: Path, installed_entrypoint: Path, existing_receipt: bool, invalid: str
) -> None:
    """Enumerated frozen admission failures precede this persistent write seam."""
    scenario = _branch_switch(tmp_path, installed_entrypoint, "local")
    receipt = _receipt(scenario)
    identity = scenario.root / ".apm-local/installation"
    if not existing_receipt:
        receipt.unlink()
        identity.unlink()
    manifest = scenario.root / "apm.yml"
    lockfile = scenario.root / "apm.lock.yaml"
    if invalid == "missing-lock":
        lockfile.unlink()
        expected = "--frozen requires apm.lock.yaml to exist"
    else:
        document = load_yaml(manifest)
        document["dependencies"]["mcp"] = [
            {
                "name": "unrecorded-service",
                "registry": False,
                "transport": "http",
                "url": "https://example.invalid/mcp",
            }
        ]
        dump_yaml(document, manifest)
        expected = "out of sync"
    before = {
        path: path.read_bytes() if path.exists() else None
        for path in (
            manifest,
            lockfile,
            identity,
            receipt,
            scenario.deployed_skill,
            scenario.installed_manifest,
        )
    }
    prior_files = {path.name: path.read_bytes() for path in receipt.parent.iterdir()}

    result = scenario.install("--frozen")

    assert result.returncode != 0
    assert expected in result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in receipt.parent.iterdir()} == prior_files
    for path, content in before.items():
        assert (path.read_bytes() if path.exists() else None) == content
