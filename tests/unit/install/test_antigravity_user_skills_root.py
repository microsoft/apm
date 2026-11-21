"""User-scope Antigravity skills deploy to ``~/.gemini/config/skills/`` (#3940).

The Antigravity App, CLI and IDE all read global skills from
``~/.gemini/config/skills/``. Earlier releases deployed them under
``~/.gemini/antigravity-cli/skills/``; the files APM recorded there must be
retired by the existing lockfile-recorded stale cleanup on the next
user-scope install, while unrecorded files are never touched. (``apm prune``
is project-scope only.)
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.component

_BASE_ENV = {"APM_E2E_TESTS": "1"}
_OLD_ROOT = ".gemini/antigravity-cli"
_NEW_ROOT = ".gemini/config"


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APM_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    monkeypatch.setattr(_conf, "_config_cache", None)


def _write_skill_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "skill-pkg"
    skill_dir = pkg / ".apm" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (pkg / "apm.yml").write_text(
        "name: skill-pkg\ndescription: test\nversion: 0.0.1\n", encoding="ascii"
    )
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests.\n---\n\nDo the thing.\n",
        encoding="ascii",
    )
    return pkg


def _write_user_manifest(home: Path, pkg: Path) -> None:
    (home / ".apm" / "apm.yml").write_text(
        textwrap.dedent(
            f"""\
            name: user
            description: user-scope test
            version: 0.0.1
            dependencies:
              apm:
                - {pkg.as_posix()}
            """
        ),
        encoding="ascii",
    )


def _run() -> None:
    result = CliRunner().invoke(
        cli,
        ["install", "-g", "--target", "antigravity"],
        env=_BASE_ENV,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _recorded_paths(home: Path) -> set[str]:
    lock = LockFile.read(home / ".apm" / "apm.lock.yaml")
    assert lock is not None
    paths: set[str] = set()
    for dep in lock.dependencies.values():
        paths.update(dep.deployed_files)
    return paths


def _seed_legacy_install(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install once with the retired root, as a pre-#3940 release did."""
    _write_user_manifest(home, _write_skill_package(tmp_path))
    with monkeypatch.context() as legacy:
        legacy.setitem(
            KNOWN_TARGETS,
            "antigravity",
            dataclasses.replace(KNOWN_TARGETS["antigravity"], user_root_dir=_OLD_ROOT),
        )
        _run()
    old_skill = home / _OLD_ROOT / "skills" / "demo-skill" / "SKILL.md"
    assert old_skill.is_file()
    assert any(p.startswith(f"{_OLD_ROOT}/skills/") for p in _recorded_paths(home))


def test_fresh_user_install_writes_skills_only_under_gemini_config(
    fake_home: Path, tmp_path: Path
) -> None:
    _write_user_manifest(fake_home, _write_skill_package(tmp_path))

    _run()

    assert (fake_home / _NEW_ROOT / "skills" / "demo-skill" / "SKILL.md").is_file()
    assert not (fake_home / _OLD_ROOT).exists()
    recorded = _recorded_paths(fake_home)
    assert any(p.startswith(f"{_NEW_ROOT}/skills/demo-skill") for p in recorded)
    assert not any(p.startswith(f"{_OLD_ROOT}/") for p in recorded)


def test_reinstall_retires_recorded_legacy_skills_only(
    fake_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_legacy_install(fake_home, tmp_path, monkeypatch)
    unrecorded = fake_home / _OLD_ROOT / "skills" / "hand-authored" / "SKILL.md"
    unrecorded.parent.mkdir(parents=True)
    unrecorded.write_text("---\nname: hand-authored\n---\n", encoding="ascii")

    _run()

    assert not (fake_home / _OLD_ROOT / "skills" / "demo-skill").exists()
    assert (fake_home / _NEW_ROOT / "skills" / "demo-skill" / "SKILL.md").is_file()
    assert unrecorded.read_text(encoding="ascii") == "---\nname: hand-authored\n---\n"
    recorded = _recorded_paths(fake_home)
    assert not any(p.startswith(f"{_OLD_ROOT}/") for p in recorded)
