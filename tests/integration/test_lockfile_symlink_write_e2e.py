"""Lockfile writes fail closed on a symlinked ``apm.lock.yaml`` (#2993).

An atomic ``os.replace`` onto a symlink replaces the link itself, so a
lockfile shared through a link would silently diverge from its target.
APM rejects symlinks in most operations; the lockfile writer refuses to
write through one and leaves both the link and its target untouched.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile, LockfileSymlinkError
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = pytest.mark.component

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"

_STALE_LOCKFILE = "lockfile_version: '1'\ndependencies: []\n"


def _symlink_or_skip(link: Path, target: str) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


def _lockfile() -> LockFile:
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="owner/repo", resolved_commit="a" * 40))
    return lock


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def test_writer_refuses_symlinked_lockfile_and_names_link_and_target(tmp_path: Path) -> None:
    target = tmp_path / "shared.lock.yaml"
    target.write_text(_STALE_LOCKFILE, encoding="utf-8")
    link = tmp_path / "apm.lock.yaml"
    _symlink_or_skip(link, "shared.lock.yaml")
    original = target.read_bytes()

    with pytest.raises(LockfileSymlinkError) as refused:
        _lockfile().write(link)

    message = str(refused.value)
    assert str(link) in message
    assert "shared.lock.yaml" in message
    assert link.is_symlink()
    assert os.readlink(link) == "shared.lock.yaml"
    assert target.read_bytes() == original


def test_writer_refuses_dangling_symlinked_lockfile(tmp_path: Path) -> None:
    link = tmp_path / "apm.lock.yaml"
    _symlink_or_skip(link, "missing.lock.yaml")

    with pytest.raises(LockfileSymlinkError):
        _lockfile().write(link)

    assert link.is_symlink()
    assert not (tmp_path / "missing.lock.yaml").exists()


def test_writer_still_writes_regular_lockfile(tmp_path: Path) -> None:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(_STALE_LOCKFILE, encoding="utf-8")

    _lockfile().write(path)

    assert not path.is_symlink()
    assert "owner/repo" in path.read_text(encoding="utf-8")


@pytest.fixture
def symlinked_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pkg = tmp_path / "pkg"
    (pkg / ".apm" / "instructions").mkdir(parents=True)
    (pkg / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    (pkg / ".apm" / "instructions" / "a.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nhello\n', encoding="utf-8"
    )
    (tmp_path / "apm.yml").write_text(
        textwrap.dedent("""\
            name: proj
            version: 1.0.0
            targets:
              - copilot
            dependencies:
              apm:
                - ./pkg
        """),
        encoding="utf-8",
    )
    (tmp_path / "shared.lock.yaml").write_text(_STALE_LOCKFILE, encoding="utf-8")
    _symlink_or_skip(tmp_path / "apm.lock.yaml", "shared.lock.yaml")
    return tmp_path


@pytest.mark.parametrize("args", [["lock"], ["install"]], ids=["lock", "install"])
def test_commands_fail_closed_on_symlinked_lockfile(symlinked_project: Path, args: list[str]):
    from apm_cli.cli import cli

    link = symlinked_project / "apm.lock.yaml"
    target = symlinked_project / "shared.lock.yaml"
    original = target.read_bytes()

    with patch(_PATCH_UPDATES, return_value=None):
        result = CliRunner().invoke(cli, args)

    output = " ".join(((result.output or "") + (result.stderr or "")).split())
    assert result.exit_code != 0, output
    assert "[x]" in output
    assert "symbolic link" in output
    assert "apm.lock.yaml" in output
    assert "shared.lock.yaml" in output
    assert "Lockfile written" not in output
    assert link.is_symlink(), "the lockfile link must not be replaced by a regular file"
    assert os.readlink(link) == "shared.lock.yaml"
    assert target.read_bytes() == original


def test_global_install_fails_closed_on_symlinked_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.cli import cli

    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    (apm_dir / "apm.yml").write_text(
        textwrap.dedent("""\
            name: global
            version: 1.0.0
            dependencies:
              mcp:
                - name: example
                  registry: false
                  transport: stdio
                  command: echo
        """),
        encoding="utf-8",
    )
    target = home / "shared.lock.yaml"
    target.write_text(_STALE_LOCKFILE, encoding="utf-8")
    link = apm_dir / "apm.lock.yaml"
    _symlink_or_skip(link, "../shared.lock.yaml")
    original = target.read_bytes()

    with patch(_PATCH_UPDATES, return_value=None):
        result = CliRunner().invoke(cli, ["install", "-g", "--target", "copilot"])

    output = " ".join(((result.output or "") + (result.stderr or "")).split())
    assert result.exit_code != 0, output
    assert "[x]" in output
    assert "symbolic link" in output
    assert "../shared.lock.yaml" in output
    assert "permissions" not in output
    assert link.is_symlink()
    assert target.read_bytes() == original


def _invoke(args: list[str]):
    from apm_cli.cli import cli

    with patch(_PATCH_UPDATES, return_value=None):
        result = CliRunner().invoke(cli, args)
    return result, " ".join(((result.output or "") + (result.stderr or "")).split())


def test_install_does_not_migrate_legacy_lockfile_onto_dangling_symlink(
    symlinked_project: Path,
) -> None:
    link = symlinked_project / "apm.lock.yaml"
    (symlinked_project / "shared.lock.yaml").unlink()
    legacy = symlinked_project / "apm.lock"
    legacy.write_text(_STALE_LOCKFILE, encoding="utf-8")
    original = legacy.read_bytes()

    result, output = _invoke(["install"])

    assert result.exit_code != 0, output
    assert "symbolic link" in output
    assert link.is_symlink()
    assert os.readlink(link) == "shared.lock.yaml"
    assert legacy.read_bytes() == original


def _project_with_two_locked_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("pkg", "pkg2"):
        pkg = tmp_path / name
        (pkg / ".apm" / "instructions").mkdir(parents=True)
        (pkg / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n", encoding="utf-8")
        (pkg / ".apm" / "instructions" / f"{name}.instructions.md").write_text(
            '---\napplyTo: "**"\n---\nhello\n', encoding="utf-8"
        )
    (tmp_path / "apm.yml").write_text(
        textwrap.dedent("""\
            name: proj
            version: 1.0.0
            targets:
              - copilot
            dependencies:
              apm:
                - ./pkg
                - ./pkg2
        """),
        encoding="utf-8",
    )
    result, output = _invoke(["install"])
    assert result.exit_code == 0, output
    (tmp_path / "apm.lock.yaml").rename(tmp_path / "shared.lock.yaml")
    _symlink_or_skip(tmp_path / "apm.lock.yaml", "shared.lock.yaml")
    clear_apm_yml_cache()
    return tmp_path


def test_uninstall_fails_closed_on_symlinked_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_with_two_locked_packages(tmp_path, monkeypatch)
    link = project / "apm.lock.yaml"
    target = project / "shared.lock.yaml"
    original = target.read_bytes()

    result, output = _invoke(["uninstall", "./pkg2"])

    assert result.exit_code != 0, output
    assert "symbolic link" in output
    assert "shared.lock.yaml" in output
    assert "Uninstall complete" not in output
    assert link.is_symlink()
    assert target.read_bytes() == original


def test_direct_mcp_install_names_symlinked_lockfile(symlinked_project: Path) -> None:
    link = symlinked_project / "apm.lock.yaml"
    target = symlinked_project / "shared.lock.yaml"
    original = target.read_bytes()

    args = ["install", "--mcp", "example", "--target", "copilot", "--", "echo", "hi"]
    with patch("sys.argv", ["apm", *args]):
        result, output = _invoke(args)

    assert result.exit_code != 0, output
    assert "symbolic link" in output
    assert "shared.lock.yaml" in output
    assert link.is_symlink()
    assert target.read_bytes() == original
