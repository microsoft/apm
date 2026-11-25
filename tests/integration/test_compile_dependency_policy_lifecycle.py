"""APMLifecycle: an org-denied dependency's content never reaches compiled agent context.

PROMISE: P-INV-I4 -- a deny never widens what reaches the agent. Once org
policy denies an installed dependency, ``apm compile`` refuses as install
does, instead of rendering that dependency's instructions into AGENTS.md.
``apm run`` and ``apm preview`` judge the package that owns a prompt the same
way, whether it is read from apm_modules or from its deployed copy (#4734).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4"),
]

_MARKER = "Prefer the org-denied house style."
_PROMPT_MARKER = "Review with the org-denied checklist."
_SCRIPT = "review"


def _package(world: World) -> str:
    """A local package named like an org-denied one, carrying one instruction."""
    root = world.root / "authored-compile" / "p-mixed"
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (root / "apm.yml").write_text(
        yaml.safe_dump({"name": "p-mixed", "version": "1.0.0"}), encoding="utf-8"
    )
    (instructions / "p-mixed.instructions.md").write_text(
        f"---\napplyTo: '**'\n---\n\n{_MARKER}\n", encoding="utf-8"
    )
    prompts = root / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "p-mixed.prompt.md").write_text(
        f"---\ndescription: Org-denied review.\n---\n\n{_PROMPT_MARKER}\n", encoding="utf-8"
    )
    return root.as_posix()


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _rendered(project: Path) -> str:
    agents = project / "AGENTS.md"
    return agents.read_text(encoding="utf-8") if agents.is_file() else ""


def _install_package(world: World) -> None:
    """Install the package for copilot and drop any AGENTS.md install produced."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["copilot"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    installed = _apm(world, "install", _package(world))
    assert installed.returncode == 0, _evidence(installed)
    (world.project / "AGENTS.md").unlink(missing_ok=True)


@pytest.mark.parametrize(
    "flags", [(), ("--force-instructions",), ("--local-only", "--force-instructions")]
)
def test_compile_refuses_to_render_an_org_denied_dependency(
    lifecycle_world: World,  # noqa: F811
    flags: tuple[str, ...],
) -> None:
    world = lifecycle_world
    _install_package(world)

    use_policy(world, "deny-all-deps")
    result = _apm(world, "compile", *flags)

    assert _MARKER not in _rendered(world.project), _evidence(result)
    assert result.returncode != 0, _evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    assert "p-mixed" in output, _evidence(result)
    assert "apm.yml" in output, _evidence(result)


def test_compile_renders_a_dependency_the_org_policy_allows(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    _install_package(world)

    use_policy(world, "allow")
    result = _apm(world, "compile", "--force-instructions")

    assert result.returncode == 0, _evidence(result)
    assert _MARKER in _rendered(world.project), _evidence(result)


def _declare_script(world: World) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["scripts"] = {_SCRIPT: "codex p-mixed.prompt.md"}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _compiled_prompts(project: Path) -> list[str]:
    compiled = project / ".apm" / "compiled"
    return [
        path.relative_to(project).as_posix()
        for path in sorted(compiled.rglob("*"))
        if path.is_file() and _PROMPT_MARKER in path.read_text(encoding="utf-8", errors="replace")
    ]


@pytest.mark.parametrize(
    "argv",
    [("preview", _SCRIPT), ("run", _SCRIPT), ("run", "p-mixed")],
    ids=["preview-script", "run-script", "run-discovered-prompt"],
)
def test_run_and_preview_refuse_an_org_denied_dependency_prompt(
    lifecycle_world: World,  # noqa: F811
    argv: tuple[str, ...],
) -> None:
    world = lifecycle_world
    _install_package(world)
    _declare_script(world)

    use_policy(world, "deny-all-deps")
    result = _apm(world, *argv)

    assert result.returncode != 0, _evidence(result)
    assert not _compiled_prompts(world.project), _evidence(result)
    assert _PROMPT_MARKER not in result.stdout + result.stderr, _evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    # The org-policy refusal, not a prompt that could not be found.
    assert "Blocked by org policy" in output, _evidence(result)
    assert "p-mixed" in output, _evidence(result)


def test_preview_compiles_a_prompt_the_org_policy_allows(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    _install_package(world)
    _declare_script(world)

    use_policy(world, "allow")
    result = _apm(world, "preview", _SCRIPT)

    assert result.returncode == 0, _evidence(result)
    assert _compiled_prompts(world.project), _evidence(result)
