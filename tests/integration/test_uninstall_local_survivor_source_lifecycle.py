"""Real-CLI lifecycle: uninstall never writes into a surviving local source (#4566).

A direct local dependency's source directory belongs to the consumer and sits
outside ``apm_modules/``. Uninstalling another package validates and restages
local survivors; a plugin-shaped source without its own ``apm.yml`` used to
be normalized in place, leaving a synthesized ``apm.yml`` and ``.apm/`` in the
consumer's tree. Any normalization now happens only on a staged copy.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-single-file-package-materialization"),
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else b"<dir>"
        for path in sorted(root.rglob("*"))
    }


def _project(root: Path, layout: str) -> Path:
    survivor = root / "vendor" / "pkg-local"
    skill = "---\nname: review\ndescription: review things\n---\nReview.\n"
    if layout == "apm-package":
        _write(survivor / "apm.yml", "name: pkg-local\nversion: 0.1.0\n")
        _write(survivor / ".apm" / "skills" / "review" / "SKILL.md", skill)
    else:
        _write(survivor / "plugin.json", '{"name": "pkg-local", "version": "0.1.0"}\n')
        _write(survivor / "skills" / "review" / "SKILL.md", skill)
        if layout == "plugin-with-apm-yml":
            _write(survivor / "apm.yml", "name: pkg-local\nversion: 0.1.0\n")
    _write(root / "vendor" / "pkg-two" / "apm.yml", "name: pkg-two\nversion: 0.1.0\n")
    _write(
        root / "vendor" / "pkg-two" / ".apm" / "instructions" / "two.instructions.md",
        '---\napplyTo: "**"\n---\nTwo.\n',
    )
    _write(
        root / "apm.yml",
        "name: consumer\nversion: 0.1.0\ndependencies:\n  apm:\n"
        "    - ./vendor/pkg-local\n    - ./vendor/pkg-two\n",
    )
    return survivor


# plugin-no-apm-yml is the #4566 cell; apm-package is the control that was already clean.
@pytest.mark.parametrize("layout", ["plugin-no-apm-yml", "plugin-with-apm-yml", "apm-package"])
def test_uninstall_leaves_a_surviving_local_source_byte_identical(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    survivor = _project(project, layout)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    env = isolated.subprocess_env()

    def run(*args: str):
        result = runner.run(args, scenario_id="4566", cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    before_install = _tree(survivor)
    run("install", "--target", "cursor", "--no-policy")
    assert _tree(survivor) == before_install, "install wrote into the local source"

    run("uninstall", "./vendor/pkg-two")

    assert _tree(survivor) == before_install
    assert (survivor / "apm.yml").exists() is (layout != "plugin-no-apm-yml")
    assert (survivor / ".apm").exists() is (layout == "apm-package")
    assert (project / "apm_modules" / "_local" / "pkg-local").is_dir()
