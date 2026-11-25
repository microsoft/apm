"""A folder under $HOME never adopts a stray ~/apm.yml as its project (#5448).

RT-5 found subdirectory discovery (#4576) walking up to a home-directory
apm.yml, editing it and deploying into ~/.claude, a user-scope directory.
Each cell drives the installed CLI with HOME pointed at a temp home.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-install-command-scope-selection", "P-INV-I2"),
]

# APM's own per-user state (APM home, deployment lock, deploy cache), not project data.
_APM_STATE = (".apm/", ".apm-local/", ".cache/")
_HOME_MANIFEST = "name: home\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: []\n"


def _home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    package = home / "src" / "pkgs" / "a"
    skill = package / ".apm" / "skills" / "a-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    (package / "apm.yml").write_text("name: a\nversion: 1.0.0\n", encoding="utf-8")
    skill.write_text("---\nname: a-skill\ndescription: a\n---\n# a\n", encoding="utf-8")
    (home / "apm.yml").write_text(_HOME_MANIFEST, encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"APM_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME"}
    }
    env.update({"HOME": str(home), "USERPROFILE": str(home), "NO_COLOR": "1"})
    return home, env


def _tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.relative_to(root).as_posix().startswith(skip)
    }


def _apm(binary: Path, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), *args, "--no-policy"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_package_install_in_a_new_folder_stays_in_that_folder(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    home, env = _home(tmp_path)
    project = home / "work" / "newproj"
    (project / ".claude").mkdir(parents=True)
    before = _tree(home, skip=("work/", *_APM_STATE))

    result = _apm(apm_binary_path, project, env, "install", "../../src/pkgs/a")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "../../src/pkgs/a" in str(load_yaml(project / "apm.yml")["dependencies"]["apm"])
    assert (project / ".claude" / "skills" / "a-skill" / "SKILL.md").is_file()
    assert _tree(home, skip=("work/", *_APM_STATE)) == before
    assert not (home / ".claude").exists()


def test_a_bare_install_in_an_empty_folder_writes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    home, env = _home(tmp_path)
    fresh = home / "work" / "fresh"
    fresh.mkdir(parents=True)
    before = _tree(home, skip=_APM_STATE)

    result = _apm(apm_binary_path, fresh, env, "install")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "No apm.yml found" in result.stdout + result.stderr
    assert _tree(home, skip=_APM_STATE) == before
    assert not (home / ".claude").exists()


def test_a_project_install_after_a_user_install_never_writes_the_home_lock(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    home, env = _home(tmp_path)
    package = home / "src" / "pkgs" / "a"
    user = _apm(apm_binary_path, home, env, "install", "-g", str(package))
    assert user.returncode == 0, user.stdout + user.stderr
    project = home / "work" / "proj"
    (project / ".claude").mkdir(parents=True)

    result = _apm(apm_binary_path, project, env, "install", "../../src/pkgs/a")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / "apm.lock.yaml").exists()
    assert (home / "apm.yml").read_text(encoding="utf-8") == _HOME_MANIFEST
    assert (project / "apm.lock.yaml").is_file()
