"""``apm audit --ci`` and ``apm install --frozen`` agree on locked package content hashes (#4187).

PROMISE: I3 -- ``install --frozen`` and ``audit --ci`` reach the same verdict
on the same lockfile state (source: .agents/skills/apm-lifecycle-tests/SKILL.md;
owner ``package-content-hash`` in .apm/architecture/owners/install-deployment.json).
CELL: OP=audit-ci|install-frozen SRC=git PRIM=apm-package SCOPE=project
LOCK=content-hash-differs-from-locked-commit DEPLOYED=warm|cold FROZEN

A lock row whose ``content_hash`` does not describe the bytes at its locked
commit is one state with one verdict: ``apm install``, ``apm install --frozen``
and ``apm audit --ci`` all fail, and none of them writes the project or the
lockfile. Restoring the recorded hash makes every command pass again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    Shape,
    apm_package,
    lock_bytes,
    tree_digest,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_TARGETS = ("copilot", "claude")
_OTHER_HASH = "sha256:" + "0" * 64


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _set_locked_content_hash(project: Path, value: str) -> str:
    lock_path = project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    rows = [row for row in lock["dependencies"] if row.get("repo_url") == "test/pkg"]
    assert len(rows) == 1, lock["dependencies"]
    recorded = rows[0]["content_hash"]
    assert recorded.startswith("sha256:") and recorded != value
    rows[0]["content_hash"] = value
    dump_yaml(lock, lock_path)
    return recorded


def _state(project: Path) -> tuple[bytes, dict[str, bytes], LifecycleStateSnapshot]:
    return (
        lock_bytes(project),
        tree_digest(project),
        LifecycleStateSnapshot.capture(project, targets=_TARGETS),
    )


@pytest.mark.parametrize("deployed", ["warm", "cold"])
def test_audit_ci_and_frozen_install_agree_on_locked_content_hash(
    tmp_path: Path, apm_binary_path: Path, deployed: str
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.manifest(Shape([world.publish("pkg", apm_package)], "test/pkg", targets=_TARGETS))

    def apm(*args: str, cwd: Path) -> CommandResult:
        result = world.apm(*args, cwd=cwd, expect=None)
        _assert_output_oracles(result)
        return result

    installed = apm("install", cwd=world.project)
    assert installed.returncode == 0, _evidence(installed)
    project = world.cold_clone() if deployed == "cold" else world.project
    baseline = apm("audit", "--ci", cwd=project)
    assert baseline.returncode == 0, _evidence(baseline)

    recorded = _set_locked_content_hash(project, _OTHER_HASH)
    before = _state(project)

    # I3 paired probe: both readers of the same lock state refuse it.
    audit = apm("audit", "--ci", cwd=project)
    assert audit.returncode != 0, _evidence(audit)
    assert "test/pkg" in audit.stdout + audit.stderr, _evidence(audit)
    assert _state(project) == before, "I5: audit --ci changed durable state"
    frozen = apm("install", "--frozen", cwd=project)
    assert frozen.returncode != 0, _evidence(frozen)
    assert _state(project) == before, "I7: refused frozen install changed durable state"

    plain = apm("install", cwd=project)
    assert plain.returncode != 0, _evidence(plain)
    assert _state(project) == before, "I7: refused install changed durable state"

    # Restoring the recorded hash converges every reader again.
    _set_locked_content_hash(project, recorded)
    for args in (("audit", "--ci"), ("install", "--frozen")):
        converged = apm(*args, cwd=project)
        assert converged.returncode == 0, _evidence(converged)
