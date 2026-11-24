"""A carried shared-skill receipt moves alone; its writer's other receipts stay (#5048).

PROMISE: P-INV-I3 -- when a re-anchor carries a shared skill to the writer a
fresh install records, only that path's receipt is re-bound; the writer's
other receipts, whose bindings the preflight never checked, are untouched.

The project is installed with the real binary; the receipt rename runs
in-process against its receipts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld

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
    for skill, body in ((f"{name}-skill", name), ("shared", "E")):
        (root / f".apm/skills/{skill}").mkdir(parents=True, exist_ok=True)
        (root / f".apm/skills/{skill}/SKILL.md").write_bytes(
            f"---\nname: {skill}\ndescription: Skill {skill}\n---\n# {body}\n".encode()
        )
    (root / "apm.yml").write_bytes(manifest.encode())


def test_carrying_a_shared_skill_leaves_the_writers_other_receipts_alone(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5048 review: only the carried path is re-bound, never every claim of its writer."""
    from apm_cli.core.deployment_reanchor import rename_receipt_owners
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "x", "x")
    _skill_package(vendor / "z", "z", ("../e", "../x"))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/z"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    (receipt,) = (world.isolated.home / ".apm" / "deployment-receipts").glob("*.json")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    other = next(key for key in data["bindings"] if key.endswith(".claude/skills/x-skill/SKILL.md"))
    data["bindings"][other] = ["local:vendor/x", "stale"]
    receipt.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    for key, value in world.env.items():
        monkeypatch.setenv(key, value)

    rename_receipt_owners(
        world.project,
        InstallScope.PROJECT,
        LockFile.read(world.project / "apm.lock.yaml"),
        {"./vendor/e": "local:vendor/e"},
        {".claude/skills/shared/SKILL.md": "local:vendor/x"},
    )

    after = json.loads(receipt.read_text(encoding="utf-8"))
    assert after["bindings"][other] == ["local:vendor/x", "stale"]
    shared = next(
        key for key in after["bindings"] if key.endswith(".claude/skills/shared/SKILL.md")
    )
    assert after["bindings"][shared][0] == "local:vendor/x"


def test_a_replay_that_raises_refuses_the_reanchor_before_any_write(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5048 review: any replay failure, not only OSError, fails closed with nothing changed."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "e", "e")
    _skill_package(vendor / "z", "z", ("../e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/z"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    before = tree_digest(world.project)
    receipts = world.isolated.home / ".apm" / "deployment-receipts"
    receipt_bytes = {path.name: path.read_bytes() for path in receipts.glob("*.json")}

    def broken(*_args, **_kwargs):
        raise RuntimeError("replay broke")

    with monkeypatch.context() as patched:
        patched.setattr("apm_cli.install.drift.run_replay", broken)
        patched.chdir(world.project)
        result = CliRunner().invoke(cli, ["uninstall", "./vendor/e"], env=world.env)

    output = " ".join(result.output.split())
    assert result.exit_code == 1, output
    assert "could not be rendered offline" in output, output
    assert "apm.yml and apm.lock.yaml were not changed" in output, output
    assert tree_digest(world.project) == before
    assert {path.name: path.read_bytes() for path in receipts.glob("*.json")} == receipt_bytes
