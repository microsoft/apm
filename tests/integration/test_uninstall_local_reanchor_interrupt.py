"""An interrupted uninstall re-anchor restores lock and receipts together (#5077).

PROMISE: P-INV-I3 -- an interrupt (Ctrl-C) after ``apm uninstall`` saved the
re-anchored package's renamed receipts rolls ``apm.yml``, the lock and the
receipts back to the same pre-command bytes, so they never name different
owners and a retried uninstall re-anchors cleanly.

The project is installed with the real binary; the interrupted uninstall
runs in-process so Ctrl-C lands right after the receipt save.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.component,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I1"),
]


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    (root / f".apm/skills/{name}-skill").mkdir(parents=True)
    (root / "apm.yml").write_bytes(manifest.encode())
    (root / f".apm/skills/{name}-skill/SKILL.md").write_bytes(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n".encode()
    )


def _receipts(world: ReplayWorld) -> dict[str, bytes]:
    directory = world.isolated.home / ".apm" / "deployment-receipts"
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json"))}


def test_an_interrupt_after_the_receipt_rename_restores_lock_and_receipts(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5077: Ctrl-C after the renamed receipts are saved puts them back with the lock."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    _skill_package(world.project / "vendor" / "e", "e")
    _skill_package(world.project / "vendor" / "d", "d", ("../e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    manifest = (world.project / "apm.yml").read_bytes()
    lock, receipts = lock_bytes(world.project), _receipts(world)
    assert receipts

    def save_then_interrupt(self) -> None:
        DeploymentMaterializationSession.flush(self)
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(
            "apm_cli.core.deployment_reanchor._ReanchorSession.flush", save_then_interrupt
        )
        patched.chdir(world.project)
        result = CliRunner().invoke(cli, ["uninstall", "./vendor/e"], env=world.env)

    assert result.exit_code != 0, result.output
    assert (world.project / "apm.yml").read_bytes() == manifest
    assert lock_bytes(world.project) == lock
    assert _receipts(world) == receipts
    # Lock and receipts agree again, so the retried uninstall re-anchors cleanly.
    retried = world.apm("uninstall", "./vendor/e")
    assert "./vendor/e stays installed because" in " ".join(
        (retried.stdout + retried.stderr).split()
    )
    world.apm("install", "--frozen")
