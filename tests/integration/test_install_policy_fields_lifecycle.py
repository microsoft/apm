"""APMLifecycle: install applies manifest.scripts and unmanaged_files deny (#4826).

PROMISE: P-INV-I7 -- a refused command leaves durable state as it found it.
Under ``enforcement: block``, ``apm install`` refuses before committing
anything when apm.yml carries a ``scripts:`` block that org policy
``manifest.scripts: deny`` forbids, or when the install would leave a file in
a governed directory that no lock row claims under ``unmanaged_files.action:
deny``. Each refusal names the policy field and the fix; audit --ci reaches
the same verdict. A control without the offending input installs.
"""

from __future__ import annotations

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.lifecycle.world.scopes import use_policy
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_HAND_WRITTEN = ".github/instructions/hand-written.instructions.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _state(world: World) -> dict[str, bytes]:
    root = world.project
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _refused(world: World, field: str, fix: str) -> str:
    before = _state(world)
    result = _apm(world, "install", world.packages["p-skill:git"].spec)
    text = _text(result)
    assert result.returncode == 1, _evidence(result)
    assert f"Org policy {field}:" in text, text
    assert fix in text, text
    assert text.isascii(), text
    assert _state(world) == before
    return text


@pytest.mark.parametrize("offending", [True, False], ids=["scripts-block", "control"])
def test_scripts_deny_refuses_an_install_with_a_scripts_block(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    if offending:
        manifest = world.project / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["scripts"] = {"hello": "echo hi"}
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    use_policy(world, "scripts-deny")

    if offending:
        text = _refused(world, "manifest.scripts: deny", "Remove the scripts: block from apm.yml")
        assert "- hello" in text, text
    else:
        result = _apm(world, "install", world.packages["p-skill:git"].spec)
        assert result.returncode == 0, _evidence(result)


@pytest.mark.parametrize("offending", [True, False], ids=["unmanaged-file", "control"])
def test_unmanaged_files_deny_refuses_an_install_that_would_leave_drift(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    if offending:
        path = world.project / _HAND_WRITTEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand written\n", encoding="utf-8")
    use_policy(world, "unmanaged-deny")

    if offending:
        text = _refused(world, "unmanaged_files.action: deny", "Move or delete each listed file")
        assert _HAND_WRITTEN in text, text
        audit = _apm(world, "audit", "--ci")
        assert audit.returncode == 1, _evidence(audit)
        assert _HAND_WRITTEN in _text(audit), _evidence(audit)
    else:
        result = _apm(world, "install", world.packages["p-skill:git"].spec)
        assert result.returncode == 0, _evidence(result)
        assert (world.project / ".agents" / "skills" / "p-skill" / "SKILL.md").is_file()
