"""Installed-CLI receipts for OpenAPM lockfile requirements (req-lk-*).

Each test runs the packaged `apm` binary against a hermetic, GitHub-shaped
local origin. In-process lockfile receipts live in
test_lockfile_behavior_reqs.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.utils.hermetic_packaged_sample import (
    DEPENDENCY,
    HermeticPackagedSample,
    create_hermetic_packaged_sample,
)

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_AGENT = Path(".apm") / "agents" / "fixture-agent.agent.md"
_DEPLOYED_AGENT = ".github/agents/fixture-agent.agent.md"
_VERIFY = {
    "install-frozen": ("install", "--frozen"),
    "audit-ci": ("audit", "--ci"),
}


def _installed(tmp_path: Path, apm_binary_path: Path) -> HermeticPackagedSample:
    sample = create_hermetic_packaged_sample(
        tmp_path, apm_binary_path=apm_binary_path, project_name="consumer"
    )
    result = sample.run(("install",), scenario_id="lk-baseline-install")
    assert result.returncode == 0, result.stdout + result.stderr
    return sample


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _locked_entry(project: Path) -> dict:
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    return next(entry for entry in lock["dependencies"] if DEPENDENCY in entry["repo_url"])


# --- req-lk-017: deployed file hashes are re-verified --------------------------


@pytest.mark.req("req-lk-017")
@pytest.mark.parametrize("command", sorted(_VERIFY))
def test_tampered_deployed_file_fails_closed_naming_both_hashes(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    """Both verifiers recompute the hash, fail, and name the file and both digests."""
    sample = _installed(tmp_path, apm_binary_path)
    project = sample.project.root
    expected = _locked_entry(project)["deployed_file_hashes"][_DEPLOYED_AGENT]
    target = project / _DEPLOYED_AGENT
    target.write_bytes(target.read_bytes() + b"\ntampered\n")
    observed = hashlib.sha256(target.read_bytes()).hexdigest()
    tampered = target.read_bytes()
    lock_before = (project / "apm.lock.yaml").read_bytes()

    result = sample.run(_VERIFY[command], scenario_id=f"lk-017-{command}")

    output = _text(result)
    assert result.returncode == 1, output
    assert _DEPLOYED_AGENT in output
    assert expected.removeprefix("sha256:")[:12] in output
    assert observed[:12] in output
    assert target.read_bytes() == tampered
    assert (project / "apm.lock.yaml").read_bytes() == lock_before


# --- req-lk-015: locked package content is recomputed and enforced -----------


@pytest.mark.req("req-lk-015")
@pytest.mark.xfail(
    strict=True,
    reason="#4187: tampered locked package content passes install --frozen and audit --ci",
)
@pytest.mark.parametrize("command", sorted(_VERIFY))
def test_tampered_package_content_fails_closed_naming_the_entry(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    sample = _installed(tmp_path, apm_binary_path)
    project = sample.project.root
    content = project / "apm_modules" / DEPENDENCY / _AGENT
    content.write_bytes(content.read_bytes() + b"\ntampered\n")

    result = sample.run(_VERIFY[command], scenario_id=f"lk-015-content-{command}")

    output = _text(result)
    assert result.returncode == 1, output
    assert DEPENDENCY in output and "content_hash" in output


@pytest.mark.req("req-lk-015")
@pytest.mark.parametrize("command", sorted(_VERIFY))
def test_locked_content_hash_is_recomputed_and_names_both_envelopes(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    """Both verifiers recompute the tree at the locked commit and name the entry and both envelopes."""
    sample = _installed(tmp_path, apm_binary_path)
    project = sample.project.root
    lock_path = project / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    entry = next(row for row in lock["dependencies"] if DEPENDENCY in row["repo_url"])
    observed = entry["content_hash"]
    expected = "sha256:" + "0" * 64
    entry["content_hash"] = expected
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    lock_before = lock_path.read_bytes()

    result = sample.run(_VERIFY[command], scenario_id=f"lk-015-lock-{command}")

    output = _text(result)
    assert result.returncode == 1, output
    assert (
        f"Content hash mismatch for {DEPENDENCY}: expected {expected}, got {observed}." in output
    ), output
    assert lock_path.read_bytes() == lock_before


# --- req-lk-018: frozen by default under CI ------------------------------------


@pytest.mark.req("req-lk-018")
@pytest.mark.xfail(strict=True, reason="#4292: CI does not select frozen install behaviour")
def test_install_under_ci_defaults_to_frozen(tmp_path: Path, apm_binary_path: Path) -> None:
    sample = create_hermetic_packaged_sample(
        tmp_path, apm_binary_path=apm_binary_path, project_name="consumer"
    )
    project = sample.project.root

    result = sample.runner.run(
        ("install",),
        scenario_id="lk-018-ci-default",
        cwd=project,
        env={**sample.environment, "CI": "true"},
    )

    output = _text(result)
    assert result.returncode == 1, output
    assert "--frozen requires apm.lock.yaml" in output
    assert not (project / "apm.lock.yaml").exists()
    assert not (project / "apm_modules").exists()
