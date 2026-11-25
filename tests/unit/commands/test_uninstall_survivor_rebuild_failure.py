"""A survivor rebuild that raises must fail the uninstall (#4954)."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-post-uninstall-dependency-reachability"),
]


REMOVED, SURVIVOR = "owner/removed", "owner/survivor"


def _project(root: Path) -> None:
    dump_yaml(
        {"name": "p", "version": "1.0.0", "dependencies": {"apm": [REMOVED, SURVIVOR]}},
        root / "apm.yml",
    )
    LockFile(
        dependencies={key: LockedDependency(repo_url=key) for key in (REMOVED, SURVIVOR)}
    ).write(root / "apm.lock.yaml")
    for key in (REMOVED, SURVIVOR):
        (root / "apm_modules" / key).mkdir(parents=True)
        (root / "apm_modules" / key / "apm.yml").write_text(
            f"name: {key.split('/')[1]}\nversion: 1.0.0\n", encoding="utf-8"
        )
    prompts = root / "apm_modules" / SURVIVOR / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "hello.prompt.md").write_text("hello", encoding="utf-8")


def test_survivor_rebuild_exception_is_recorded_as_a_package_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caught exception fails the uninstall and loses no survivor state."""
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    with patch(
        "apm_cli.install.services.integrate_package_primitives",
        side_effect=RuntimeError("rebuild blew up"),
    ):
        result = CliRunner().invoke(uninstall, [REMOVED])

    assert result.exit_code != 0, result.output
    assert "Uninstall incomplete: 1 package failed" in result.output
    lock = LockFile.read(tmp_path / "apm.lock.yaml")
    assert lock is not None
    assert SURVIVOR in lock.dependencies
    assert SURVIVOR in (tmp_path / "apm.yml").read_text(encoding="utf-8")
    assert (tmp_path / "apm_modules" / SURVIVOR / "apm.yml").is_file()


def test_a_rebuild_that_records_an_error_before_raising_fails_the_package_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error the rebuild recorded, then its exception, count one failed package.

    A host-local survivor's identity is its absolute path while uninstall labels
    it ``_local/<name>``, so a second error under that label would double count.
    """
    source = tmp_path / "src" / "survivor"
    (source / ".apm" / "prompts").mkdir(parents=True)
    (source / "apm.yml").write_text("name: survivor\nversion: 1.0.0\n", encoding="utf-8")
    (source / ".apm" / "prompts" / "hello.prompt.md").write_text("hello", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    dump_yaml(
        {"name": "p", "version": "1.0.0", "dependencies": {"apm": [REMOVED, str(source)]}},
        project / "apm.yml",
    )
    LockFile(
        dependencies={
            REMOVED: LockedDependency(repo_url=REMOVED),
            "_local/survivor": LockedDependency(
                repo_url="_local/survivor", source="local", local_path=str(source)
            ),
        }
    ).write(project / "apm.lock.yaml")
    (project / "apm_modules" / REMOVED).mkdir(parents=True)
    (project / "apm_modules" / REMOVED / "apm.yml").write_text(
        "name: removed\nversion: 1.0.0\n", encoding="utf-8"
    )
    shutil.copytree(source, project / "apm_modules" / "_local" / "survivor")
    monkeypatch.chdir(project)

    def record_then_raise(*_args, diagnostics, package_name, **_kwargs):
        diagnostics.error("partial write failed", package=package_name)
        raise RuntimeError("rebuild blew up")

    with patch(
        "apm_cli.install.services.integrate_package_primitives",
        side_effect=record_then_raise,
    ):
        result = CliRunner().invoke(uninstall, [REMOVED])

    output = " ".join(result.output.split())
    assert result.exit_code != 0, output
    assert "Uninstall incomplete: 1 package failed" in output, output
    assert "2 packages failed" not in output, output


def test_uninstall_command_fails_when_survivor_rebuild_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command names the failed survivor, exits non-zero and never says complete."""
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    with patch(
        "apm_cli.install.services.integrate_package_primitives",
        side_effect=RuntimeError("rebuild blew up"),
    ):
        result = CliRunner().invoke(uninstall, [REMOVED])

    assert result.exit_code != 0, result.output
    assert SURVIVOR in result.output
    assert "Could not rebuild the surviving package's integrated files" in result.output
    assert "Uninstall incomplete: 1 package failed" in result.output
    assert "Uninstall complete" not in result.output
