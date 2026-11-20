"""APMLifecycle: install --frozen and audit --ci agree on hook approval pins (#4188).

PROMISE: I3 -- ``apm install --frozen`` and ``apm audit --ci`` reach the same
verdict, with the same remediation (source: .agents/skills/apm-lifecycle-tests,
owner ``executable-item-admission`` in .apm/architecture/owners/install-deployment.json).
PROMISE: I4 -- an approval that no longer admits a hook file never leaves that
hook deployed or recorded after the next install.
CELL: OP=install FROZEN SRC=local PRIM=hook SCOPE=project GATE=pin-mismatch TGT=claude|copilot

``apm approve`` pins each hook file of a package to its content. When the pin
in ``apm.yml`` no longer matches the package content, the trust owner
(``security/trust.py``) parks the hook file. ``install --frozen`` refuses
before anything deploys, exactly like ``audit --ci``, and a plain
``apm install`` retires the hook from every config and from the lockfile.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-executable-trust-fingerprint"),
]

_PACKAGE = "../hooky"
_HOOK_ITEM = ".apm/hooks/guard.json"
_SCRIPT = "hooky-guard.sh"
# One merged-config target and one file-per-hook target; the owner decides
# before any target is written, so the verdict does not vary by target.
_TARGETS = tuple(
    name for name in ("claude", "copilot") if "hooks" in KNOWN_TARGETS[name].primitives
)
_UNMATCHED_PIN = "sha256:" + "0" * 64


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    (hooks / "scripts").mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    document = (
        '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": '
        f'[{{"type": "command", "command": "bash ./scripts/{_SCRIPT}"}}]}}]}}}}\n'
    )
    (hooks / "guard.json").write_text(document, encoding="ascii")
    (hooks / "scripts" / _SCRIPT).write_text("#!/bin/bash\necho guard\n", encoding="ascii")


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _text(result: CommandResult) -> str:
    """Command output with terminal wrapping undone."""
    return " ".join((result.stdout + result.stderr).split())


def _hook_residue(project: Path) -> list[str]:
    """Every deployed file that is, or still references, the package script."""
    residue = []
    for path in project.rglob("*"):
        rel = path.relative_to(project)
        if not path.is_file() or "apm_modules" in rel.parts:
            continue
        if rel.as_posix() in {"apm.yml", "apm.lock.yaml"}:
            continue
        if path.name == _SCRIPT or _SCRIPT in path.read_text(encoding="utf-8"):
            residue.append(rel.as_posix())
    return sorted(residue)


def _lock_hook_records(project: Path) -> list[str]:
    """Lockfile rows that still claim the package's hook script."""
    lock_text = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    return [line.strip() for line in lock_text.splitlines() if _SCRIPT in line]


def _exec_status(project: Path) -> str | None:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


class _World:
    def __init__(self, tmp_path: Path, binary: Path, target: str) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
        self.env = isolated.subprocess_env()
        _hook_package(isolated.work_root / "hooky")
        self.project = isolated.work_root / "consumer"
        self.project.mkdir()
        self.target = target
        dump_yaml(
            {
                "name": "consumer",
                "version": "0.1.0",
                "targets": [target],
                "dependencies": {"apm": [_PACKAGE]},
                "executables": {},
            },
            self.project / "apm.yml",
        )
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)

    def run(self, *args: str) -> CommandResult:
        result = self.runner.run(args, cwd=self.project, env=self.env)
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)  # I6
        return result

    def ok(self, *args: str) -> CommandResult:
        result = self.run(*args)
        assert result.returncode == 0, _evidence(result)
        return result

    def snap(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(self.project, targets=(self.target,))

    def approve_and_deploy(self) -> None:
        self.ok("install")
        assert _exec_status(self.project) == "gated_pending_approval"
        assert "at these exact definitions" in _text(self.ok("approve", _PACKAGE))
        self.ok("install")
        assert _exec_status(self.project) == "deployed"
        assert _hook_residue(self.project), "the approved hook must deploy"
        converged = self.snap()
        self.ok("install", "--frozen")
        self.ok("audit", "--ci")
        assert self.snap() == converged, "I2: a converged frozen install changed state"

    def unmatch_pin(self) -> None:
        manifest_path = self.project / "apm.yml"
        manifest = load_yaml(manifest_path)
        pins = manifest["executables"]["allow"][_PACKAGE]["hooks"]
        assert set(pins) == {_HOOK_ITEM}, pins
        pins[_HOOK_ITEM] = _UNMATCHED_PIN
        dump_yaml(manifest, manifest_path)


@pytest.mark.parametrize("target", _TARGETS)
def test_frozen_and_audit_refuse_alike_when_a_hook_pin_does_not_match(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    world = _World(tmp_path, apm_binary_path, target)
    world.approve_and_deploy()
    world.unmatch_pin()
    before = world.snap()
    residue = _hook_residue(world.project)

    audit = world.run("audit", "--ci")
    assert audit.returncode != 0, _evidence(audit)
    assert world.snap() == before, "audit --ci changed durable state"

    frozen = world.run("install", "--frozen")
    assert frozen.returncode != 0, _evidence(frozen)
    # I7: the refusal happens before anything deploys or retires.
    assert world.snap() == before, "a refused frozen install changed durable state"
    assert _hook_residue(world.project) == residue

    # I3: one owner verdict, one remediation, on both surfaces.
    for result in (audit, frozen):
        text = _text(result)
        assert f"hook file '{_HOOK_ITEM}'" in text, _evidence(result)
        assert f"apm approve {_PACKAGE}" in text, _evidence(result)


@pytest.mark.parametrize("target", _TARGETS)
def test_install_retires_a_hook_whose_pin_no_longer_matches(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    world = _World(tmp_path, apm_binary_path, target)
    world.approve_and_deploy()
    world.unmatch_pin()

    parked = _text(world.ok("install"))
    assert f"Parked hook file '{_HOOK_ITEM}'" in parked, parked
    assert _exec_status(world.project) == "gated_pending_approval"
    # I4: no config entry, script or lockfile record outlives the approval.
    assert _hook_residue(world.project) == []
    assert _lock_hook_records(world.project) == []

    # I3 on the converged state, and I2 for the frozen replay.
    converged = world.snap()
    world.ok("install", "--frozen")
    world.ok("audit", "--ci")
    assert world.snap() == converged

    # Re-approving the current content deploys the hook again.
    world.ok("approve", _PACKAGE)
    world.ok("install")
    assert _exec_status(world.project) == "deployed"
    assert _hook_residue(world.project)
    world.ok("install", "--frozen")
    world.ok("audit", "--ci")
