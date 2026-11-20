"""Installed-CLI journey: every next-step hint `apm init` prints works when run.

Promise (init output owner, ``src/apm_cli/commands/init.py``): each command the
"Next Steps" panel names reaches its stated outcome when a user pastes it into
the project ``apm init`` just created, with placeholders filled in; a command
that refuses fails with a message, never an empty error line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_HINT = re.compile(r"(apm [^|]+?)\s*\|?\s*$")


def _run(binary: Path, cwd: Path, env: dict[str, str], argv: list[str], answers: str = ""):
    return subprocess.run(
        [str(binary), *argv],
        cwd=cwd,
        env=env,
        input=answers,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _package(root: Path) -> Path:
    package = root / "pkg"
    skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo skill\n---\n\nBody.\n", encoding="utf-8")
    dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    return package


def _fill(hint: str, package: Path) -> list[str]:
    command = (
        hint.replace("<owner>/<repo>", str(package))
        .replace("<harness>", "claude")
        .replace("<name>", "my-plugin")
    )
    assert "<" not in command, f"unfilled placeholder in hint: {hint}"
    return command.split()[1:]


_OUTCOMES = {
    "apm install": lambda project: (project / "apm.lock.yaml").is_file(),
    "apm plugin init": lambda project: any(project.rglob("plugin.json")),
    "apm marketplace init": lambda project: (
        "marketplace:" in (project / "apm.yml").read_text(encoding="utf-8")
    ),
}


def _reached_outcome(hint: str, project: Path) -> bool:
    for prefix, reached in _OUTCOMES.items():
        if hint.startswith(prefix):
            return reached(project)
    raise AssertionError(f"no stated outcome is known for hint: {hint}")


def test_every_init_next_step_hint_succeeds(apm_binary_path: Path, tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env(overrides={"COLUMNS": "80"})
    template = isolated.work_root / "template"
    template.mkdir()
    package = _package(isolated.package_root)

    init = _run(apm_binary_path, template, env, ["init", "-y"])
    assert init.returncode == 0, init.stdout + init.stderr

    panel = init.stdout[init.stdout.index("Next Steps") :]
    hints = [match.group(1).strip() for line in panel.splitlines() if (match := _HINT.search(line))]
    assert hints, init.stdout

    for index, hint in enumerate(hints):
        project = isolated.work_root / f"hint-{index}"
        shutil.copytree(template, project)
        # A user pasting the hint accepts every interactive default.
        result = _run(apm_binary_path, project, env, _fill(hint, package), answers="\n" * 20)
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"hint failed: {hint}\n{output}"
        assert "[x]" not in output, f"hint reported an error: {hint}\n{output}"
        assert _reached_outcome(hint, project), f"hint did not do what it says: {hint}\n{output}"
    footer = "Docs: https://microsoft.github.io/apm  |  Star: https://github.com/microsoft/apm"
    assert any(footer in line for line in init.stdout.splitlines()), init.stdout


def test_declined_plugin_scaffold_overwrite_reports_why(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "project"
    project.mkdir()
    assert _run(apm_binary_path, project, env, ["init", "-y"]).returncode == 0
    before = (project / "apm.yml").read_bytes()

    result = _run(apm_binary_path, project, env, ["plugin", "init"])

    assert result.returncode == 1
    assert not re.search(r"Error initializing project:\s*$", result.stdout + result.stderr, re.M)
    assert (project / "apm.yml").read_bytes() == before
